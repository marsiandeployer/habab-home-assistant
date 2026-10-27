"""Owner-scoped mailbox state for the Android native voice route action."""

from __future__ import annotations

import asyncio
import time
import uuid

TARGETS = frozenset({"phone", "bluetooth", "toggle", "wake_on", "wake_off"})
ONLINE_SECONDS = 8.0
COMMAND_TTL_SECONDS = 45.0
COMMAND_LEASE_SECONDS = 7.0
MAX_COMMAND_DELIVERIES = 4


class VoiceRouteMailbox:
    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._owners = {}

    def _state(self, owner):
        return self._owners.setdefault(owner, {
            "last_seen": 0.0, "client": {}, "pending": None, "last_result": None,
        })

    def _expire(self, state):
        command = state["pending"]
        if command is None or self._clock() - command["created_at"] <= COMMAND_TTL_SECONDS:
            return
        state["last_result"] = {
            "command_id": command["id"], "requested_target": command["target"],
            "status": "error", "actual_route": state["client"].get("actual_route", "unknown"),
            "input_type": state["client"].get("input_type", -1),
            "error": "route_command_timeout", "updated_at": self._clock(),
        }
        state["pending"] = None
        command["event"].set()

    def online(self, owner):
        state = self._state(owner)
        self._expire(state)
        return self._clock() - state["last_seen"] <= ONLINE_SECONDS

    def client_active(self, owner):
        state = self._state(owner)
        return self.online(owner) and bool(state["client"].get("active_voice"))

    def enqueue(self, owner, target):
        if target not in TARGETS:
            raise ValueError("invalid_voice_route_target")
        if not self.online(owner):
            raise ValueError("android_voice_unavailable")
        state = self._state(owner)
        if state["pending"] is not None:
            raise ValueError("voice_route_command_pending")
        client = state["client"]
        wants_bluetooth = (target == "bluetooth"
                           or target == "toggle" and client["selected_target"] == "phone"
                           or target == "wake_on" and client["selected_target"] in {"bluetooth", "duplicate"})
        if wants_bluetooth and not client["headset_available"]:
            raise ValueError("headset_unavailable")
        command = {"id": str(uuid.uuid4()), "target": target, "claimed_at": 0.0,
                   "deliveries": 0, "created_at": self._clock(),
                   "event": asyncio.Event()}
        state["pending"] = command
        return command["id"], command["event"]

    def poll(self, owner, client, ack=None):
        if not isinstance(client, dict):
            raise ValueError("invalid_voice_route_state")
        if (type(client.get("safe_to_switch")) is not bool
                or type(client.get("active_voice")) is not bool
                or type(client.get("wake_enabled")) is not bool
                or type(client.get("headset_available")) is not bool
                or not isinstance(client.get("phase"), str)
                or len(client["phase"]) > 32
                or not isinstance(client.get("selected_target"), str)
                or client["selected_target"] not in {"phone", "bluetooth", "duplicate"}
                or not isinstance(client.get("actual_route"), str)
                or len(client["actual_route"]) > 64
                or type(client.get("input_type")) is not int):
            raise ValueError("invalid_voice_route_state")
        state = self._state(owner)
        self._expire(state)
        state["last_seen"] = self._clock()
        state["client"] = dict(client)
        ack_recorded = False
        ack_unknown = False
        command = state["pending"]
        if ack is not None:
            if not isinstance(ack, dict) or not isinstance(ack.get("id"), str):
                raise ValueError("invalid_voice_route_ack")
            result = ack.get("result")
            if result not in {"applied", "saved", "error"}:
                raise ValueError("invalid_voice_route_ack")
            if (not isinstance(ack.get("actual_route"), str)
                    or len(ack["actual_route"]) > 64
                    or type(ack.get("input_type")) is not int
                    or type(ack.get("wake_enabled")) is not bool
                    or not isinstance(ack.get("error"), str)
                    or len(ack["error"]) > 64):
                raise ValueError("invalid_voice_route_ack")
            last = state["last_result"]
            if last is not None and ack["id"] == last["command_id"]:
                identical = all(ack.get(key) == last.get(last_key) for key, last_key in (
                    ("result", "status"), ("actual_route", "actual_route"),
                    ("input_type", "input_type"), ("error", "error"),
                    ("wake_enabled", "wake_enabled"),
                ))
                if identical or last.get("error") == "route_command_timeout":
                    ack_recorded = True  # Idempotent retry after a lost HTTP response.
                else:
                    raise ValueError("invalid_voice_route_ack")
            elif command is not None and ack["id"] == command["id"]:
                requested = command["target"]
                if (result == "applied" and requested in {"phone", "bluetooth"}
                        and ack.get("actual_route") != requested):
                    raise ValueError("voice_route_ack_mismatch")
                if result == "applied" and requested == "wake_on" and not ack["wake_enabled"]:
                    raise ValueError("voice_route_ack_mismatch")
                if result == "applied" and requested == "wake_off" and ack["wake_enabled"]:
                    raise ValueError("voice_route_ack_mismatch")
                state["last_result"] = {
                    "command_id": command["id"], "requested_target": requested,
                    "status": result, "actual_route": ack["actual_route"],
                    "input_type": ack["input_type"], "error": ack["error"],
                    "wake_enabled": ack["wake_enabled"], "updated_at": self._clock(),
                }
                state["pending"] = None
                command["event"].set()
                ack_recorded = True
                command = None
            else:
                # HA may restart after Android saved an ACK but before its response arrived.
                # Tell that same owner to discard only the unknown local ACK and keep polling.
                ack_unknown = True
                ack_unknown_id = ack["id"]
        can_redeliver = (command is not None and command["deliveries"] < MAX_COMMAND_DELIVERIES
                         and self._clock() - command["claimed_at"] >= COMMAND_LEASE_SECONDS)
        if command is not None and client["safe_to_switch"] and can_redeliver:
            command["claimed_at"] = self._clock()
            command["deliveries"] += 1
            command_result = {"id": command["id"], "target": command["target"]}
        else:
            command_result = None
        response = {"command": command_result, "ack_recorded": ack_recorded,
                "status": self.status(owner)}
        if ack_unknown:
            response.update({"ack_unknown": True, "ack_unknown_id": ack_unknown_id})
        return response

    def status(self, owner):
        state = self._state(owner)
        if not self.online(owner):
            return {"status": "unavailable", "actual_route": "unknown",
                    "wake_enabled": False, "pending": state["pending"] is not None,
                    "last_result": state["last_result"]}
        client = state["client"]
        return {"status": "online", "actual_route": client["actual_route"],
                "selected_target": client["selected_target"],
                "input_type": client["input_type"], "wake_enabled": client["wake_enabled"],
                "headset_available": client["headset_available"],
                "phase": client["phase"], "pending": state["pending"] is not None,
                "last_result": state["last_result"]}
