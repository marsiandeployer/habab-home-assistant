"""Chat drafts; only the administrator's web card calls native configuration APIs.

Pinned native seams: Core 2026.10.2 EditAutomationConfigView and input_boolean
storage collection. This module never writes YAML or invokes an automation.
"""
import asyncio
from copy import deepcopy
import hashlib
import json
import secrets
import time

import probatio

from homeassistant.components import websocket_api
from homeassistant.components.automation.config import async_validate_config_item
from homeassistant.components.config.automation import EditAutomationConfigView
from homeassistant.config import AUTOMATION_CONFIG_PATH
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv, entity_registry
from homeassistant.helpers.storage import Store

from .const import DOMAIN


MAX_DRAFTS = 64
DRAFT_TTL = 1800
SAFE_ERRORS = {
    "unauthorized", "stale_session", "unknown_draft", "invalid_draft",
    "draft_limit", "draft_expired", "draft_not_pending", "already_exists",
    "readback_mismatch", "readback_pending", "cleanup_not_allowed",
    "native_api_unavailable", "confirmation_required",
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


class AutomationSetup:
    """Bounded owner/conversation drafts with durable native-write receipts."""

    def __init__(self, hass):
        self.hass = hass
        self.drafts = {}
        self._lock = asyncio.Lock()
        self._store = None
        self._storage_error = False
        # Read using the same native class as /api/config/automation/config/ID.
        # Writes are exclusively the actual user's callApi POST in the web card.
        self._view = EditAutomationConfigView(
            "automation", "config", AUTOMATION_CONFIG_PATH, cv.string,
            data_validator=async_validate_config_item,
        )

    async def setup(self):
        self._store = Store(self.hass, 1, f"{DOMAIN}.automation_drafts")
        try:
            data = await self._store.async_load()
            records = data.get("drafts", []) if isinstance(data, dict) else []
            if not isinstance(records, list) or len(records) > MAX_DRAFTS:
                raise ValueError("invalid_draft")
            for record in records:
                config = json.loads(record["config_json"])
                if (record["kind"] not in {"automation", "input_boolean"}
                        or record["phase"] not in {"draft", "saving", "saved", "deleting", "deleted", "cancelled", "expired", "dismissed"}
                        or not isinstance(record["owner"], str) or not record["owner"]
                        or not isinstance(record["session_id"], str) or not record["session_id"]
                        or _digest(config) != record["config_hash"]
                        or (record["kind"] == "automation" and (config.get("id") != f"habab_{record['draft_id']}" or config.get("initial_state") is not False))
                        or (record["kind"] == "input_boolean" and config.get("initial") is not False)):
                    raise ValueError("invalid_draft")
                record["before_ids"] = set(record.get("before_ids", []))
                self.drafts[record["draft_id"]] = record
        except Exception:
            # Do not replace a broken journal with an empty successful history.
            self._storage_error = True

    async def _persist(self):
        if self._store is None or self._storage_error:
            raise ValueError("native_api_unavailable")
        records = []
        for record in self.drafts.values():
            value = dict(record)
            if "before_ids" in value:
                value["before_ids"] = sorted(value["before_ids"])
            records.append(value)
        try:
            await self._store.async_save({"drafts": records})
        except Exception as error:
            self._storage_error = True
            raise ValueError("native_api_unavailable") from error

    async def _scope(self, owner, session_id):
        user = await self.hass.auth.async_get_user(owner)
        if user is None or not user.is_active or not user.is_admin:
            raise ValueError("unauthorized")
        if self._store is None or self._storage_error:
            raise ValueError("native_api_unavailable")
        assistant = self.hass.data[DOMAIN].get("assistant")
        if (not isinstance(session_id, str) or not session_id
                or assistant is None or assistant.web_sessions.get(owner) != session_id):
            raise ValueError("stale_session")

    def _current(self, owner, session_id):
        assistant = self.hass.data[DOMAIN].get("assistant")
        if assistant is None or assistant.web_sessions.get(owner) != session_id:
            raise ValueError("stale_session")

    def _record(self, owner, session_id, draft_id, *, recovery=False):
        self._current(owner, session_id)
        record = self.drafts.get(draft_id)
        if record is None or record["owner"] != owner or (record["session_id"] != session_id and not recovery):
            raise ValueError("unknown_draft")
        if record["phase"] == "draft" and time.time() - record["created"] > DRAFT_TTL:
            record["phase"] = "expired"
        return record

    def _public(self, record):
        result = {key: record[key] for key in (
            "draft_id", "kind", "phase", "config_hash", "summary"
        )}
        result["config"] = json.loads(record["config_json"])
        for key in ("native_id", "entity_id", "verification", "native_config_hash", "native_normalization"):
            if key in record:
                result[key] = record[key]
        result["can_dismiss"] = (record["phase"] == "saving" and not record.get("config_present")
                                 and time.time() - record.get("prepared", record["created"]) >= 30)
        return result

    async def _automations(self):
        result = await self._view.read_config(self.hass)
        if not isinstance(result, list):
            raise ValueError("native_api_unavailable")
        return result

    async def _helpers(self):
        result = await Store(self.hass, 1, "input_boolean").async_load()
        items = result.get("items", []) if isinstance(result, dict) else []
        if not isinstance(items, list):
            raise ValueError("native_api_unavailable")
        return items

    async def draft(self, owner, session_id, body, *, helper=False):
        await self._scope(owner, session_id)
        if set(body) - ({"action", "name", "summary"} if helper else {"action", "config", "summary"}):
            raise ValueError("invalid_draft")
        summary = body.get("summary")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 2000:
            raise ValueError("invalid_draft")
        draft_id = secrets.token_hex(16)
        if helper:
            name = body.get("name")
            if not isinstance(name, str) or not name.strip() or len(name) > 120:
                raise ValueError("invalid_draft")
            # Native create has no caller-supplied ID. A draft-specific name
            # allows exact lost-response reconciliation without adopting another
            # user's concurrent same-name helper. The owner reviews this name.
            config = {"name": f"{name.strip()} · {draft_id[:8]}", "initial": False}
            kind = "input_boolean"
        else:
            config = deepcopy(body.get("config"))
            if (not isinstance(config, dict) or "id" in config
                    or not isinstance(config.get("alias"), str) or not config["alias"].strip()):
                raise ValueError("invalid_draft")
            config.update(id=f"habab_{draft_id}", initial_state=False)
            if len(_json(config).encode()) > 12000:
                raise ValueError("invalid_draft")
            try:
                # Core's validator mutates legacy singular keys into the exact
                # plural keys persisted by its native POST handler. Freeze that
                # native form, rather than hashing the pre-validation input.
                await async_validate_config_item(self.hass, config["id"], config)
            except Exception as error:
                raise ValueError("invalid_draft") from error
            kind = "automation"
        async with self._lock:
            self._current(owner, session_id)
            # Never discard a pending write/cleanup receipt during uncertain outcomes.
            for key, record in list(self.drafts.items()):
                if (record["phase"] in {"draft", "cancelled", "expired", "deleted"}
                        and time.time() - record["created"] > DRAFT_TTL):
                    del self.drafts[key]
            if len(self.drafts) >= MAX_DRAFTS:
                raise ValueError("draft_limit")
            record = {
                "draft_id": draft_id, "owner": owner, "session_id": session_id,
                "kind": kind, "phase": "draft", "created": time.time(),
                "config_json": _json(config), "config_hash": _digest(config),
                "summary": summary.strip(),
            }
            self.drafts[draft_id] = record
            await self._persist()
            self._current(owner, session_id)
            return self._public(record)

    async def status(self, owner, session_id):
        await self._scope(owner, session_id)
        keys = [key for key, record in self.drafts.items()
                if (record["owner"], record["session_id"]) == (owner, session_id)]
        views = [await self._status_view(owner, session_id, key) for key in keys]
        self._current(owner, session_id)
        return {"drafts": views}

    async def _status_view(self, owner, session_id, draft_id, *, recovery=False):
        record = self._record(owner, session_id, draft_id, recovery=recovery)
        try:
            if record["phase"] in {"saving", "saved", "dismissed"}:
                return await self.verify(owner, session_id, draft_id, recovery=recovery)
            if record["phase"] == "deleting":
                return await self.cleanup_verify(owner, session_id, draft_id)
            return self._public(record)
        except ValueError as error:
            if str(error) != "readback_mismatch":
                raise
            # A changed object must not keep a successful receipt or prevent the
            # owner reviewing other drafts. This is an explicit failed readback.
            result = {**self._public(record), "phase": "error", "error": "readback_mismatch"}
            result.pop("verification", None)
            return result

    async def ui_status(self, owner, session_id):
        """Previous receipts are visible only to the owner's protected web UI.

        Original conversation IDs stay immutable. Recovery permits readback and
        exact cleanup, never approval or saving from a replacement conversation.
        """
        result = await self.status(owner, session_id)
        keys = [key for key, record in self.drafts.items()
                if record["owner"] == owner and record["session_id"] != session_id
                and record["phase"] in {"saving", "saved", "deleting", "dismissed"}]
        recovered = []
        for key in keys:
            view = await self._status_view(owner, session_id, key, recovery=True)
            if view["phase"] != "dismissed":
                recovered.append({**view, "from_previous_chat": True})
        self._current(owner, session_id)
        return {**result, "recovery_drafts": recovered}

    async def prepare(self, owner, session_id, draft_id, config_hash):
        """Called only by authenticated web WS, never from the model bridge."""
        await self._scope(owner, session_id)
        async with self._lock:
            record = self._record(owner, session_id, draft_id)
            if record["config_hash"] != config_hash:
                raise ValueError("invalid_draft")
            if record["phase"] != "draft":
                raise ValueError("draft_not_pending")
            config = json.loads(record["config_json"])
            if record["kind"] == "automation":
                if any(item.get("id") == config["id"] for item in await self._automations()):
                    raise ValueError("already_exists")
                record["native_id"] = config["id"]
            else:
                items = await self._helpers()
                if any(item.get("name") == config["name"] for item in items):
                    raise ValueError("already_exists")
                registry = entity_registry.async_get(self.hass)
                record["before_ids"] = {item["id"] for item in items} | {
                    entry.unique_id for entry in registry.entities.values()
                    if entry.platform == "input_boolean"
                }
            self._current(owner, session_id)
            record.update(phase="saving", prepared=time.time())
            # Persist intent before the browser receives authority to create.
            await self._persist()
            self._current(owner, session_id)
            return self._public(record)

    async def acknowledge(self, owner, session_id, draft_id, native_id):
        """Bind a real native helper-create response, without trusting a model ID."""
        await self._scope(owner, session_id)
        async with self._lock:
            record = self._record(owner, session_id, draft_id)
            if (record["kind"] != "input_boolean" or record["phase"] != "saving"
                    or not isinstance(native_id, str) or not native_id or len(native_id) > 255
                    or native_id in record["before_ids"]
                    or record.get("native_id") not in (None, native_id)):
                raise ValueError("readback_mismatch")
            record["native_id"] = native_id
            await self._persist()
            self._current(owner, session_id)
            return self._public(record)

    async def _readback(self, record):
        config = json.loads(record["config_json"])
        if record["kind"] == "automation":
            matches = [item for item in await self._automations() if item.get("id") == config["id"]]
            if not matches:
                record["config_present"] = False
                return False
            if len(matches) != 1:
                raise ValueError("readback_mismatch")
            native_hash = _digest(matches[0])
            if native_hash != record["config_hash"]:
                # Recovery for already-issued immutable drafts: derive the same
                # native normalization without rewriting their approved bytes or
                # hash. Accept only the exact result of Core's own validator.
                native_expected = deepcopy(config)
                await async_validate_config_item(self.hass, config["id"], native_expected)
                if native_hash != _digest(native_expected):
                    raise ValueError("readback_mismatch")
                record["native_normalization"] = "core_automation_schema"
            record["native_config_hash"] = native_hash
        else:
            items = await self._helpers()
            if record.get("native_id"):
                referenced = [item for item in items if item.get("id") == record["native_id"]]
                if referenced and (len(referenced) != 1 or
                        {key: value for key, value in referenced[0].items() if key != "id"} != config):
                    raise ValueError("readback_mismatch")
            matches = [item for item in items
                       if item.get("id") not in record["before_ids"]
                       and (record.get("native_id") is None or item.get("id") == record["native_id"])
                       and {key: value for key, value in item.items() if key != "id"} == config]
            if not matches:
                record["config_present"] = False
                return False
            if len(matches) != 1:
                raise ValueError("readback_mismatch")
            if record.get("native_id") not in (None, matches[0]["id"]):
                raise ValueError("readback_mismatch")
            record["native_id"] = matches[0]["id"]
        record["config_present"] = True
        entity_id = entity_registry.async_get(self.hass).async_get_entity_id(
            record["kind"], record["kind"], record["native_id"])
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is None or state.state != "off":
            return False
        record["entity_id"] = entity_id
        return True

    async def verify(self, owner, session_id, draft_id, *, recovery=False):
        await self._scope(owner, session_id)
        async with self._lock:
            record = self._record(owner, session_id, draft_id, recovery=recovery)
            if record["phase"] not in {"saving", "saved", "dismissed"}:
                raise ValueError("confirmation_required")
            before = self._progress(record)
            # Native helper collection delays persistence by 10 seconds. Return an
            # honest pending receipt; the card's bounded reconciliation polls it.
            matched = await self._readback(record)
            self._current(owner, session_id)
            if matched:
                record.update(phase="saved", verification="native_config_and_off_state")
            elif record["phase"] != "dismissed":
                record["phase"] = "saving"
                record.pop("verification", None)
            if before != self._progress(record):
                await self._persist()
            self._current(owner, session_id)
            return self._public(record)

    @staticmethod
    def _progress(record):
        return tuple(record.get(key) for key in ("phase", "native_id", "entity_id", "verification", "config_present", "native_config_hash", "native_normalization"))

    async def dismiss_absent(self, owner, session_id, draft_id):
        """Close an absent intent, retaining a tombstone for any late result."""
        await self._scope(owner, session_id)
        async with self._lock:
            record = self._record(owner, session_id, draft_id, recovery=True)
            if (record["phase"] != "saving"
                    or time.time() - record.get("prepared", record["created"]) < 30):
                raise ValueError("cleanup_not_allowed")
            config = json.loads(record["config_json"])
            items = await (self._automations() if record["kind"] == "automation" else self._helpers())
            if any(item.get("id") == record.get("native_id") or (
                    record["kind"] == "input_boolean" and item.get("name") == config["name"])
                   for item in items):
                raise ValueError("cleanup_not_allowed")
            registry = entity_registry.async_get(self.hass)
            if record.get("entity_id") and self.hass.states.get(record["entity_id"]) is not None:
                raise ValueError("cleanup_not_allowed")
            if any(entry.platform == record["kind"] and (
                    entry.unique_id == record.get("native_id") or (
                        record["kind"] == "input_boolean" and
                        config["name"] in (getattr(entry, "name", None), getattr(entry, "original_name", None))))
                   for entry in registry.entities.values()):
                raise ValueError("cleanup_not_allowed")
            self._current(owner, session_id)
            record.update(phase="dismissed", verification="native_absence_readback")
            await self._persist()
            self._current(owner, session_id)
            return self._public(record)

    async def cancel(self, owner, session_id, draft_id):
        await self._scope(owner, session_id)
        async with self._lock:
            record = self._record(owner, session_id, draft_id)
            if record["phase"] not in {"draft", "expired", "cancelled"}:
                raise ValueError("draft_not_pending")
            record["phase"] = "cancelled"
            await self._persist()
            self._current(owner, session_id)
            return self._public(record)

    async def cleanup_prepare(self, owner, session_id, draft_id, config_hash):
        """Offer deletion only for this owner's exact, created, still-off object."""
        await self._scope(owner, session_id)
        async with self._lock:
            record = self._record(owner, session_id, draft_id, recovery=True)
            if (record["phase"] not in {"saved", "saving"} or record["config_hash"] != config_hash
                    or not await self._readback(record)):
                raise ValueError("cleanup_not_allowed")
            self._current(owner, session_id)
            record["phase"] = "deleting"
            record.pop("verification", None)
            await self._persist()
            self._current(owner, session_id)
            return self._public(record)

    async def cleanup_verify(self, owner, session_id, draft_id):
        await self._scope(owner, session_id)
        async with self._lock:
            record = self._record(owner, session_id, draft_id, recovery=True)
            if record["phase"] not in {"deleting", "deleted"}:
                raise ValueError("cleanup_not_allowed")
            before = self._progress(record)
            items = await (self._automations() if record["kind"] == "automation" else self._helpers())
            remaining = any(item.get("id") == record["native_id"] for item in items)
            state = self.hass.states.get(record.get("entity_id"))
            self._current(owner, session_id)
            if not remaining and state is None:
                record.update(phase="deleted", verification="native_absence_readback")
            if before != self._progress(record):
                await self._persist()
            self._current(owner, session_id)
            return self._public(record)


@callback
def async_register_commands(hass):
    websocket_api.async_register_command(hass, automation_action)


@websocket_api.websocket_command({
    probatio.Required("type"): "habab_home/automation/action",
    probatio.Required("operation"): probatio.In([
        "status", "prepare", "acknowledge", "verify", "cancel", "dismiss_absent", "cleanup_prepare", "cleanup_verify"
    ]),
    probatio.Required("session_id"): probatio.All(str, probatio.Length(min=1, max=128)),
    probatio.Optional("draft_id"): probatio.All(str, probatio.Length(min=32, max=32)),
    probatio.Optional("config_hash"): probatio.All(str, probatio.Length(min=64, max=64)),
    probatio.Optional("native_id"): probatio.All(str, probatio.Length(min=1, max=255)),
})
@websocket_api.require_admin
@websocket_api.async_response
async def automation_action(hass, connection, msg):
    user = connection.user
    if user is None or not user.is_active or not user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "unauthorized")
        return
    try:
        controller = hass.data[DOMAIN]["automation_setup"]
        operation = "ui_status" if msg["operation"] == "status" else msg["operation"]
        args = [user.id, msg["session_id"]]
        if operation != "ui_status":
            if "draft_id" not in msg:
                raise ValueError("invalid_draft")
            args.append(msg["draft_id"])
        if operation in {"prepare", "cleanup_prepare"}:
            if "config_hash" not in msg:
                raise ValueError("invalid_draft")
            args.append(msg["config_hash"])
        if operation == "acknowledge":
            if "native_id" not in msg:
                raise ValueError("invalid_draft")
            args.append(msg["native_id"])
        result = await getattr(controller, operation)(*args)
        connection.send_result(msg["id"], result)
    except Exception as error:
        code = str(error) if isinstance(error, ValueError) and str(error) in SAFE_ERRORS else "native_api_unavailable"
        connection.send_error(msg["id"], code, code)
