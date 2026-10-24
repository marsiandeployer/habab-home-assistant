"""Shared authenticated front door for the web and Telegram setup chats."""

import asyncio
import hashlib
import json
import re

from homeassistant.helpers.storage import Store

from .agent_diagnostics import AgentDiagnostics
from .assist_adapter import AssistAdapter
from .setup_agent import (
    AgentError, VOICE_TURN_EVENT_ID_KEY, VOICE_TURN_HASH_KEY,
    VOICE_TURN_METADATA_KEY, VOICE_TURN_METADATA_VALUE,
)

VOICE_EVENT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


class AssistantService:
    """Own channel-specific session maps and delegate to the selected runtime."""

    def __init__(self, hass, runtime):
        self.hass = hass
        self.runtime = runtime
        # Keep the established keys so existing owner histories survive upgrades.
        self.web_store = Store(hass, 1, "habab_home.agent_sessions", private=True)
        self.telegram_store = Store(hass, 1, "habab_home.telegram_sessions", private=True)
        self.web_context_store = Store(
            hass, 1, "habab_home.web_history_contexts", private=True)
        self.voice_receipt_store = Store(
            hass, 1, "habab_home.voice_turn_receipts", private=True)
        self.web_sessions = {}
        self.web_contexts = {}
        self.telegram_sessions = {}
        self.voice_receipts = {}
        self.web_history_cache = {}
        self.voice_journal_lock = asyncio.Lock()
        self.web_lifecycle_lock = asyncio.Lock()
        self.diagnostics = runtime.diagnostics

    async def setup(self):
        self.web_sessions = await self.web_store.async_load() or {}
        self.web_contexts = await self.web_context_store.async_load() or {}
        self.telegram_sessions = await self.telegram_store.async_load() or {}
        self.voice_receipts = await self.voice_receipt_store.async_load() or {}
        changed = False
        owners = set(self.web_sessions)
        for receipt in self.voice_receipts.values():
            if (isinstance(receipt, dict) and isinstance(receipt.get("owner"), str)
                    and type(receipt.get("epoch")) is int and receipt["epoch"] >= 0):
                owners.add(receipt["owner"])
        for owner in owners:
            stored = self.web_contexts.get(owner)
            if (not isinstance(stored, dict) or type(stored.get("epoch")) is not int
                    or stored.get("epoch") < 0):
                epoch = max((receipt.get("epoch", 0) for receipt in self.voice_receipts.values()
                             if isinstance(receipt, dict) and receipt.get("owner") == owner
                             and type(receipt.get("epoch")) is int
                             and receipt.get("epoch") >= 0), default=0)
                self.web_contexts[owner] = {
                    "session_id": self.web_sessions.get(owner), "epoch": epoch}
            elif stored.get("session_id") != self.web_sessions.get(owner):
                # Recover an interrupted session-map/context commit without
                # reviving leases that still name the older map.
                self.web_contexts[owner] = {
                    "session_id": self.web_sessions.get(owner),
                    "epoch": stored["epoch"] + 1,
                }
                changed = True
        if changed:
            await self.web_context_store.async_save(self.web_contexts)

    def _web_context(self, owner):
        context = self.web_contexts.get(owner)
        if isinstance(context, dict) and type(context.get("epoch")) is int:
            return context
        return {"session_id": self.web_sessions.get(owner), "epoch": 0}

    async def _save_web_context(self, owner, session_id, epoch):
        updated = dict(self.web_contexts)
        updated[owner] = {"session_id": session_id, "epoch": epoch}
        await self.web_context_store.async_save(updated)
        self.web_contexts = updated

    async def _require_active_admin(self, owner):
        user = await self.hass.auth.async_get_user(owner)
        if not user or not user.is_active or not user.is_admin:
            raise AgentError("not_allowed")

    def _telegram_key(self, owner, entry_id, chat_id):
        return f"{owner}:{entry_id}:{chat_id}"

    async def _session(self, mapping, store, key, title, new=False):
        if self.runtime.lock.locked():
            raise AgentError("busy")
        async with self.runtime.lock:
            await self.runtime.ensure()
            if new or key not in mapping:
                created = await self.runtime.request("POST", "session", {"title": title})
                mapping[key] = created["id"]
                await store.async_save(mapping)
            session_id = mapping[key]
            messages = await self.runtime.request(
                "GET", f"session/{session_id}/message?limit=60")
            return {"session_id": session_id, "messages": self.runtime.visible(messages)}

    async def web_session(self, owner, new=False):
        await self._require_active_admin(owner)
        async with self.web_lifecycle_lock:
            await self._require_active_admin(owner)
            async with self.voice_journal_lock:
                old_session = self.web_sessions.get(owner)
                old_context = dict(self._web_context(owner))
                if owner not in self.web_contexts:
                    await self._save_web_context(
                        owner, old_context["session_id"], old_context["epoch"])
                old_epoch = old_context["epoch"]
            if new and old_session:
                await self._materialize_pending_voice(owner, old_session, old_epoch)
            result = await self._session(self.web_sessions, self.web_store, owner,
                                         "Home Assistant", new)
            target_epoch = old_epoch + 1 if new and old_session else old_epoch
            async with self.voice_journal_lock:
                if self._web_context(owner) != old_context:
                    raise AgentError("not_found")
                target_context = {"session_id": result["session_id"],
                                  "epoch": target_epoch}
                if target_context != old_context:
                    try:
                        await self._save_web_context(
                            owner, result["session_id"], target_epoch)
                    except Exception:
                        if old_session is not None:
                            self.web_sessions[owner] = old_session
                        else:
                            self.web_sessions.pop(owner, None)
                        await self.web_store.async_save(self.web_sessions)
                        raise
            if new and old_session:
                await self._materialize_pending_voice(owner, old_session, old_epoch)
            epoch = target_epoch
            await self._materialize_pending_voice(owner, result["session_id"], epoch)
            result["messages"] = self.web_history_cache.get(
                (owner, result["session_id"]), result["messages"])
            result["messages"] = self._project_pending_voice(
                owner, result["session_id"], result["messages"], epoch)
            return result

    async def voice_history_context(self, owner):
        """Return the current private web history lease without starting OpenCode."""
        await self._require_active_admin(owner)
        async with self.voice_journal_lock:
            await self._require_active_admin(owner)
            context = self._web_context(owner)
            return {"session_id": context["session_id"], "epoch": context["epoch"]}

    async def telegram_session(self, owner, entry_id, chat_id, new=False):
        await self._require_active_admin(owner)
        key = self._telegram_key(owner, entry_id, chat_id)
        return await self._session(self.telegram_sessions, self.telegram_store, key,
                                   "Telegram Home Assistant", new)

    async def telegram_reset(self, owner, entry_id, chat_id):
        await self._require_active_admin(owner)
        key = self._telegram_key(owner, entry_id, chat_id)
        if self.runtime.active == (owner, self.telegram_sessions.get(key)):
            raise AgentError("busy")
        return await self._session(self.telegram_sessions, self.telegram_store, key,
                                   "Telegram Home Assistant", new=True)

    async def telegram_forget(self, owner, entry_id, chat_id):
        key = self._telegram_key(owner, entry_id, chat_id)
        session_id = self.telegram_sessions.pop(key, None)
        # Revocation is cleanup, so it must still work after the HA account was
        # disabled. The exact owner+bot+chat mapping scopes any active abort.
        if session_id and self.runtime.active == (owner, session_id):
            await self.runtime.abort(owner, session_id)
        await self.telegram_store.async_save(self.telegram_sessions)
        return session_id

    def _mapped_session(self, owner, channel, session_id, entry_id=None, chat_id=None):
        if channel == "web":
            return self.web_sessions.get(owner) == session_id
        if channel == "telegram" and entry_id is not None and chat_id is not None:
            key = self._telegram_key(owner, entry_id, chat_id)
            return self.telegram_sessions.get(key) == session_id
        return False

    async def trusted_origin(self, owner, session_id):
        """Resolve private setup origin from the active, mapped runtime session."""
        if self.runtime.active != (owner, session_id):
            return None
        await self._require_active_admin(owner)
        if self.runtime.active != (owner, session_id):
            return None
        if self.web_sessions.get(owner) == session_id:
            return None
        prefix = f"{owner}:"
        matches = []
        for key, mapped in self.telegram_sessions.items():
            if mapped != session_id or not key.startswith(prefix):
                continue
            parts = key[len(prefix):].split(":")
            if len(parts) == 2 and all(parts):
                matches.append(parts)
        if len(matches) != 1:
            return None
        entry_id, chat_id = matches[0]
        if (len(entry_id) > 128 or ":" in entry_id or len(chat_id) > 32
                or not chat_id.lstrip("-").isdigit()):
            return None
        telegram = self.hass.data.get("habab_telegram")
        if telegram is None:
            return None
        entry = telegram.entry_for_owner(owner)
        binding = telegram.bindings.get(entry_id)
        state = telegram.entries.get(entry_id)
        bot_id = entry.data.get("bot_id") if entry is not None and entry.entry_id == entry_id else None
        if (type(bot_id) is not int or bot_id <= 0 or not binding or not state
                or binding.get("bot_id") != bot_id or str(binding.get("chat_id")) != chat_id
                or not state.get("ready") or state.get("chat_id") != chat_id
                or state.get("user_id") != binding.get("user_id")):
            member_origin = await telegram.member_origin(owner, entry_id, chat_id, session_id)
            if (member_origin is None or self.runtime.active != (owner, session_id)
                    or self.telegram_sessions.get(self._telegram_key(owner, entry_id, chat_id)) != session_id):
                return None
            await self._require_active_admin(owner)
            return member_origin
        return {"channel": "telegram", "owner": owner,
                "entry_id": entry_id, "chat_id": chat_id, "bot_id": bot_id}

    async def run(self, owner, channel, session_id, text, entry_id=None, chat_id=None):
        await self._require_active_admin(owner)
        if not self._mapped_session(owner, channel, session_id, entry_id, chat_id):
            raise AgentError("not_found")
        if channel == "web":
            async with self.web_lifecycle_lock:
                await self._require_active_admin(owner)
                if not self._mapped_session(owner, channel, session_id):
                    raise AgentError("not_found")
                async with self.voice_journal_lock:
                    epoch = self._web_context(owner)["epoch"]
                await self._materialize_pending_voice(owner, session_id, epoch)
        result = await self.runtime.run(owner, session_id, text)
        if channel == "web":
            async with self.voice_journal_lock:
                if self._mapped_session(owner, channel, session_id):
                    canonical_messages = result.get("messages", [])
                    result["messages"] = self._project_pending_voice(
                        owner, session_id, canonical_messages,
                        self._web_context(owner)["epoch"])
                    self.web_history_cache[(owner, session_id)] = list(canonical_messages)
        return result

    async def record_voice_turn(self, owner, session_id, event_id, user_text,
                                assistant_text, *, history_epoch=None):
        """Durably journal an observed Realtime pair without starting OpenCode."""
        if (not isinstance(owner, str) or not owner or len(owner) > 128
                or (session_id is not None and (not isinstance(session_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id)))
                or not isinstance(event_id, str) or not VOICE_EVENT_ID.fullmatch(event_id)
                or not isinstance(user_text, str) or not user_text.strip() or len(user_text) > 4000
                or not isinstance(assistant_text, str) or not assistant_text.strip()
                or len(assistant_text) > 12000
                or type(history_epoch) is not int or history_epoch < 0):
            raise AgentError("invalid_voice_turn")
        transcript = json.dumps(
            {"user": user_text, "assistant": assistant_text},
            ensure_ascii=False, separators=(",", ":"))
        payload_digest = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
        receipt_key = json.dumps([owner, history_epoch, event_id],
                                 ensure_ascii=False, separators=(",", ":"))
        async with self.voice_journal_lock:
            await self._require_active_admin(owner)
            context = dict(self._web_context(owner))
            current_epoch = context["epoch"]
            current_session = context["session_id"]
            if history_epoch != current_epoch or (session_id is not None
                    and session_id != current_session):
                raise AgentError("not_found")
            receipt = self.voice_receipts.get(receipt_key)
            if receipt is not None:
                if (not isinstance(receipt, dict)
                        or receipt.get("payload_sha256") != payload_digest):
                    raise AgentError("voice_event_conflict")
                duplicate = True
            else:
                receipt = {
                    "owner": owner, "epoch": history_epoch,
                    "session_id": current_session, "event_id": event_id,
                    "payload_sha256": payload_digest, "state": "pending",
                    "user_text": user_text, "assistant_text": assistant_text,
                }
                updated = dict(self.voice_receipts)
                updated[receipt_key] = receipt
                await self.voice_receipt_store.async_save(updated)
                self.voice_receipts = updated
                duplicate = False
            await self._require_active_admin(owner)
            if context != self._web_context(owner):
                raise AgentError("not_found")
            current_session = self._web_context(owner)["session_id"]
            if receipt.get("state") == "pending" and receipt.get("session_id") is None:
                if current_session:
                    updated = dict(self.voice_receipts)
                    rebound = dict(receipt, session_id=current_session)
                    updated[receipt_key] = rebound
                    await self.voice_receipt_store.async_save(updated)
                    self.voice_receipts = updated
                    receipt = rebound
            messages = []
            if current_session:
                messages = self._project_pending_voice(
                    owner, current_session,
                    self.web_history_cache.get((owner, current_session), []), history_epoch)
            return {"session_id": current_session, "messages": messages,
                    "recorded": True, "duplicate": duplicate,
                    "pending": receipt.get("state") == "pending"}

    def _project_pending_voice(self, owner, session_id, messages, epoch):
        projected = list(messages or [])
        for receipt in self.voice_receipts.values():
            if (isinstance(receipt, dict) and receipt.get("state") == "pending"
                    and receipt.get("owner") == owner and receipt.get("epoch") == epoch
                    and receipt.get("session_id") == session_id):
                projected.extend([
                    {"role": "user", "text": receipt.get("user_text", "")},
                    {"role": "assistant", "text": receipt.get("assistant_text", "")},
                ])
        return projected[-60:]

    async def _materialize_pending_voice(self, owner, session_id, epoch):
        """Flush durable voice pairs to canonical OpenCode history on web demand."""
        async with self.voice_journal_lock:
            pending = [(key, value) for key, value in self.voice_receipts.items()
                       if isinstance(value, dict) and value.get("state") == "pending"
                       and value.get("owner") == owner
                       and (value.get("session_id") == session_id
                            or (value.get("session_id") is None
                                and value.get("epoch") == epoch))]
            if not pending:
                return
            unbound = [(key, dict(receipt, session_id=session_id))
                       for key, receipt in pending if receipt.get("session_id") is None]
            if unbound:
                updated = dict(self.voice_receipts)
                updated.update(unbound)
                await self.voice_receipt_store.async_save(updated)
                self.voice_receipts = updated
                pending = [(key, self.voice_receipts[key]) for key, _ in pending]
        async with self.runtime.lock:
            await self.runtime.ensure()
            messages = await self.runtime.request(
                "GET", f"session/{session_id}/message?limit=2000")
            if not isinstance(messages, list):
                raise AgentError("runtime_error")
            completions = []
            for key, receipt in pending:
                transcript = json.dumps(
                    {"user": receipt["user_text"], "assistant": receipt["assistant_text"]},
                    ensure_ascii=False, separators=(",", ":"))
                event_id = receipt["event_id"]
                digest = receipt["payload_sha256"]
                found = False
                for message in messages:
                    for part in message.get("parts", []):
                        metadata = part.get("metadata")
                        if (isinstance(metadata, dict)
                                and metadata.get(VOICE_TURN_METADATA_KEY) == VOICE_TURN_METADATA_VALUE
                                and metadata.get(VOICE_TURN_EVENT_ID_KEY) == event_id):
                            if (part.get("synthetic") is not True
                                    or metadata.get(VOICE_TURN_HASH_KEY) != digest
                                    or part.get("text") != transcript):
                                raise AgentError("voice_event_conflict")
                            found = True
                if not found:
                    await self.runtime.request("POST", f"session/{session_id}/message", {
                        "noReply": True,
                        "parts": [{"type": "text", "text": transcript,
                                   "synthetic": True, "metadata": {
                                       VOICE_TURN_METADATA_KEY: VOICE_TURN_METADATA_VALUE,
                                       VOICE_TURN_EVENT_ID_KEY: event_id,
                                       VOICE_TURN_HASH_KEY: digest,
                                   }}],
                    })
                    messages = await self.runtime.request(
                        "GET", f"session/{session_id}/message?limit=2000")
                    found = any(
                        p.get("synthetic") is True
                        and isinstance(p.get("metadata"), dict)
                        and p["metadata"].get(VOICE_TURN_METADATA_KEY) == VOICE_TURN_METADATA_VALUE
                        and p["metadata"].get(VOICE_TURN_EVENT_ID_KEY) == event_id
                        and p["metadata"].get(VOICE_TURN_HASH_KEY) == digest
                        and p.get("text") == transcript
                        for m in messages if isinstance(m, dict)
                        for p in m.get("parts", []) if isinstance(p, dict))
                    if not found:
                        raise AgentError("runtime_error")
                completed = dict(receipt)
                completed.update(state="complete", session_id=session_id)
                completed.pop("user_text", None)
                completed.pop("assistant_text", None)
                completions.append((key, completed))
            async with self.voice_journal_lock:
                updated = dict(self.voice_receipts)
                for key, completed in completions:
                    current = updated.get(key)
                    if (isinstance(current, dict) and current.get("state") == "pending"
                            and current.get("payload_sha256") == completed["payload_sha256"]):
                        updated[key] = completed
                await self.voice_receipt_store.async_save(updated)
                self.voice_receipts = updated
            self.web_history_cache[(owner, session_id)] = self.runtime.visible(messages)

    async def abort(self, owner, channel, session_id, entry_id=None, chat_id=None):
        await self._require_active_admin(owner)
        if not self._mapped_session(owner, channel, session_id, entry_id, chat_id):
            raise AgentError("not_found")
        return await self.runtime.abort(owner, session_id)

    async def diagnostics_for_web(self, owner, session_id):
        await self._require_active_admin(owner)
        if not self._mapped_session(owner, "web", session_id):
            raise AgentError("not_found")
        process = self.runtime.process
        state = "running" if process is not None and process.returncode is None else "stopped"
        return {
            "session_id": session_id,
            "active": self.runtime.active == (owner, session_id),
            "runtime_status": state,
            "error": self.diagnostics.get(owner, session_id),
        }

    async def run_assist(self, entry, chat_log):
        """Preserve native Assist's narrow exposed-tool runtime and permissions."""
        await AssistAdapter(self.hass, entry.entry).complete(chat_log, entry.entity_id)
