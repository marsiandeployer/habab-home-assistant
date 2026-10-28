"""Shared authenticated front door for the web and Telegram setup chats."""

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone

from homeassistant.helpers.storage import Store
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .agent_diagnostics import AgentDiagnostics
from .assist_adapter import AssistAdapter
from .const import DOMAIN
from .effective_provider import effective_provider_entry
from .fast_dialog import MAX_USER_CHARS, bounded_history, complete_dialogue, dialogue_part
from .providers import ProviderError
from .diagnostics_sessions import (
    normalize_telegram_session_index,
    normalize_web_session_index,
    remember_telegram_session,
    remember_web_session,
)
from .setup_agent import (
    AgentError, VOICE_TURN_EVENT_ID_KEY, VOICE_TURN_HASH_KEY,
    VOICE_TURN_METADATA_KEY, VOICE_TURN_METADATA_VALUE,
)
from .voice_history import (
    VOICE_EVENT_ID_KEY,
    VOICE_EVENT_HASH_KEY,
    VOICE_EVENT_METADATA_KEY,
    VOICE_EVENT_PROTOCOL,
    encode_voice_event,
    decode_voice_event_part,
    valid_voice_event_fields,
)

VOICE_EVENT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_LOGGER = logging.getLogger(__name__)


def bounded_voice_history(messages):
    """Budget text including the two JSON string layers in the voice setup."""
    remaining = 16000
    history = []
    def cost(text):
        return len(json.dumps(json.dumps(text, ensure_ascii=False), ensure_ascii=False).encode("utf-8"))
    for item in reversed(bounded_history(messages)):
        text = item["content"]
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if cost(text[:middle]) <= remaining:
                low = middle
            else:
                high = middle - 1
        if not low:
            break
        text = text[:low]
        history.append({"role": item["role"], "content": text})
        remaining -= cost(text)
    return list(reversed(history))


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
        self.web_diagnostics_store = Store(
            hass, 1, "habab_home.web_diagnostics_sessions", private=True)
        self.telegram_diagnostics_store = Store(
            hass, 1, "habab_home.telegram_diagnostics_sessions", private=True)
        self.voice_receipt_store = Store(
            hass, 1, "habab_home.voice_turn_receipts", private=True)
        self.web_sessions = {}
        self.web_contexts = {}
        self.web_diagnostics_sessions = {}
        self.telegram_diagnostics_sessions = {}
        self.telegram_sessions = {}
        self.voice_receipts = {}
        self.web_history_cache = {}
        self.voice_journal_lock = asyncio.Lock()
        self.web_lifecycle_lock = asyncio.Lock()
        self.dialogue_tasks = {}
        self.diagnostics = runtime.diagnostics

    async def setup(self):
        self.web_sessions = await self.web_store.async_load() or {}
        self.web_contexts = await self.web_context_store.async_load() or {}
        self.web_diagnostics_sessions = normalize_web_session_index(
            await self.web_diagnostics_store.async_load())
        self.telegram_diagnostics_sessions = normalize_telegram_session_index(
            await self.telegram_diagnostics_store.async_load())
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

    async def _archive_web_session(self, owner, session_id):
        """Persist only bounded session IDs and archive times, never transcript text."""
        updated = remember_web_session(
            self.web_diagnostics_sessions, owner, session_id,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        if updated is self.web_diagnostics_sessions:
            return
        await self.web_diagnostics_store.async_save(updated)
        self.web_diagnostics_sessions = updated

    @staticmethod
    def _telegram_scope_id(owner, entry_id, chat_id):
        key = f"{owner}:{entry_id}:{chat_id}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    async def _archive_telegram_session(self, owner, session_id, scope_id):
        """Persist only bounded Telegram session IDs and archive times."""
        updated = remember_telegram_session(
            self.telegram_diagnostics_sessions, owner, session_id,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            scope_id,
        )
        if updated is self.telegram_diagnostics_sessions:
            return
        await self.telegram_diagnostics_store.async_save(updated)
        self.telegram_diagnostics_sessions = updated

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
                await self._archive_web_session(owner, old_session)
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
            self.web_history_cache[(owner, result["session_id"])] = list(result["messages"])
            result["messages"] = self._project_pending_voice(
                owner, result["session_id"], result["messages"], epoch)
            return result

    async def voice_history_context(self, owner, *, include_messages=False):
        """Read the owner's lease, optionally including bounded current-chat text."""
        await self._require_active_admin(owner)
        if include_messages:
            # Opening voice must not create a chat. Reuse the exact UI session,
            # including voice turns not yet materialized into OpenCode history.
            async with self.voice_journal_lock:
                context = dict(self._web_context(owner))
                session_id = context["session_id"]
                messages = self.web_history_cache.get((owner, session_id))
            if session_id and messages is None:
                # A running command may take minutes; voice must not wait for it.
                if self.runtime.lock.locked():
                    raise AgentError("busy")
                async with self.runtime.lock:
                    await self.runtime.ensure()
                    stored = await self.runtime.request(
                        "GET", f"session/{session_id}/message?limit=60")
                    if not isinstance(stored, list):
                        raise AgentError("runtime_error")
                    messages = self.runtime.visible(stored)
            async with self.voice_journal_lock:
                await self._require_active_admin(owner)
                if context != self._web_context(owner):
                    raise AgentError("not_found")
                # A web turn may have finished while the cold read was in flight.
                messages = self.web_history_cache.get((owner, session_id), messages)
                if session_id:
                    self.web_history_cache[(owner, session_id)] = messages
                projected = self._project_pending_voice(
                    owner, session_id, messages or [], context["epoch"])
                return {**context, "messages": bounded_voice_history(projected)}
        async with self.voice_journal_lock:
            await self._require_active_admin(owner)
            context = self._web_context(owner)
            return {"session_id": context["session_id"], "epoch": context["epoch"]}

    async def telegram_session(self, owner, entry_id, chat_id, new=False):
        await self._require_active_admin(owner)
        key = self._telegram_key(owner, entry_id, chat_id)
        old_session = self.telegram_sessions.get(key)
        if new and old_session:
            await self._archive_telegram_session(
                owner, old_session, self._telegram_scope_id(owner, entry_id, chat_id))
        return await self._session(self.telegram_sessions, self.telegram_store, key,
                                   "Telegram Home Assistant", new)

    async def telegram_reset(self, owner, entry_id, chat_id):
        await self._require_active_admin(owner)
        key = self._telegram_key(owner, entry_id, chat_id)
        if self.runtime.active == (owner, self.telegram_sessions.get(key)):
            raise AgentError("busy")
        return await self.telegram_session(owner, entry_id, chat_id, new=True)

    async def telegram_forget(self, owner, entry_id, chat_id):
        key = self._telegram_key(owner, entry_id, chat_id)
        session_id = self.telegram_sessions.pop(key, None)
        # Revocation is cleanup, so it must still work after the HA account was
        # disabled. The exact owner+bot+chat mapping scopes any active abort.
        await self.telegram_store.async_save(self.telegram_sessions)
        scope_id = self._telegram_scope_id(owner, entry_id, chat_id)
        records = self.telegram_diagnostics_sessions.get(owner, [])
        # Older entries without a scope cannot be safely attributed to this chat.
        kept = [record for record in records
                if record.get("scope_id") not in {None, scope_id}]
        if len(kept) != len(records):
            updated = dict(self.telegram_diagnostics_sessions)
            if kept:
                updated[owner] = kept
            else:
                updated.pop(owner, None)
            await self.telegram_diagnostics_store.async_save(updated)
            self.telegram_diagnostics_sessions = updated
        if session_id and self.runtime.active == (owner, session_id):
            await self.runtime.abort(owner, session_id)
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

    async def run(self, owner, channel, session_id, text, entry_id=None, chat_id=None,
                  expected_history_epoch=None, *, executor_only=False):
        await self._require_active_admin(owner)
        if not self._mapped_session(owner, channel, session_id, entry_id, chat_id):
            raise AgentError("not_found")
        if channel == "web":
            async with self.web_lifecycle_lock:
                await self._require_active_admin(owner)
                if not self._mapped_session(owner, channel, session_id):
                    raise AgentError("not_found")
                async with self.voice_journal_lock:
                    context = dict(self._web_context(owner))
                    epoch = context["epoch"]
                    if (context["session_id"] != session_id
                            or (expected_history_epoch is not None
                                and (type(expected_history_epoch) is not int
                                     or expected_history_epoch != epoch))):
                        raise AgentError("not_found")
                await self._materialize_pending_voice(owner, session_id, epoch)
                try:
                    result = await self._run_text(
                        owner, channel, session_id, text, executor_only=executor_only)
                except (Exception, asyncio.CancelledError):
                    # The runtime may have persisted a user turn before failing.
                    # Next voice/UI read must reload it instead of reusing old text.
                    async with self.voice_journal_lock:
                        self.web_history_cache.pop((owner, session_id), None)
                    raise
                async with self.voice_journal_lock:
                    if self._mapped_session(owner, channel, session_id):
                        canonical_messages = result.get("messages", [])
                        result["messages"] = self._project_pending_voice(
                            owner, session_id, canonical_messages,
                            self._web_context(owner)["epoch"])
                        self.web_history_cache[(owner, session_id)] = list(canonical_messages)
                return result
        return await self._run_text(
            owner, channel, session_id, text, entry_id, chat_id, executor_only=executor_only)

    async def _run_text(self, owner, channel, session_id, text, entry_id=None, chat_id=None,
                        *, executor_only=False):
        """Select a visible product mode; model errors never fall back to OpenCode."""
        entry = effective_provider_entry(self.hass.config_entries.async_entries(DOMAIN))
        if entry is None:
            raise AgentError("provider_required")
        if executor_only or entry.options.get("fast_dialog", True) is False:
            started = time.monotonic()
            result = await self.runtime.run(owner, session_id, text)
            result["dialogue"] = {"mode": "opencode", "route": "opencode",
                                  "total_ms": round((time.monotonic() - started) * 1000)}
            return result
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_USER_CHARS:
            raise AgentError("invalid_request")
        started = time.monotonic()
        trace = {"mode": "fast_dialog", "route": "pending"}

        async def require_scope():
            if state["aborted"]:
                raise AgentError("aborted")
            await self._require_active_admin(owner)
            if not self._mapped_session(owner, channel, session_id, entry_id, chat_id):
                raise AgentError("not_found")
            current = effective_provider_entry(self.hass.config_entries.async_entries(DOMAIN))
            if current is not entry or dict(current.data) != settings or dict(current.options) != options:
                raise AgentError("not_found")

        settings, options = dict(entry.data), dict(entry.options)
        if self.runtime.lock.locked():
            raise AgentError("busy")
        task_key = (owner, session_id)
        state = {"task": asyncio.current_task(), "aborted": False}
        self.dialogue_tasks[task_key] = state
        try:
            async with self.runtime.lock:
                await require_scope()
                contract = await self.runtime.ensure(settings)
                await require_scope()
                messages = await self.runtime.request(
                    "GET", f"session/{session_id}/message?limit=60")
                if not isinstance(messages, list):
                    raise AgentError("runtime_error")
                history = self.runtime.visible(messages)
                await require_scope()
                model_started = time.monotonic()
                try:
                    route, answer = await complete_dialogue(
                        self.hass, async_get_clientsession(self.hass), settings, history, text,
                        contract=contract)
                finally:
                    trace["provider_ms"] = round((time.monotonic() - model_started) * 1000)
                    await require_scope()
                trace["route"] = route
                if route == "direct":
                    part = dialogue_part(text, answer)
                    previous_ids = {message.get("info", {}).get("id") for message in messages}
                    await self.runtime.request("POST", f"session/{session_id}/message", {
                        "noReply": True, "parts": [part],
                    })
                    stored = await self.runtime.request(
                        "GET", f"session/{session_id}/message?limit=60")
                    await require_scope()
                    if (not isinstance(stored, list) or not any(
                            message.get("info", {}).get("id") not in previous_ids
                            and any(isinstance(saved, dict) and all(
                                saved.get(key) == value for key, value in part.items())
                                for saved in message.get("parts", [])) for message in stored)):
                        raise AgentError("runtime_error")
                    result = {"session_id": session_id, "messages": self.runtime.visible(stored)}
            if route == "opencode":
                await require_scope()
                result = await self.runtime.run(owner, session_id, text)
                await require_scope()
            trace["total_ms"] = round((time.monotonic() - started) * 1000)
            self.diagnostics.clear(owner, session_id)
            result["dialogue"] = trace
            return result
        except asyncio.CancelledError:
            trace["route"] = "cancelled"
            if state["aborted"]:
                raise AgentError("aborted") from None
            raise
        except ProviderError as error:
            # Provider codes are internal enums. Never retain provider response text.
            safe_code = error.code if error.code in {
                "timeout", "quota", "invalid_auth", "invalid_model", "invalid_response",
                "cannot_connect", "provider_not_ready", "tools_unsupported",
            } else "invalid_response"
            self.diagnostics.record(owner, session_id, {"name": "ProviderError", "code": safe_code})
            trace.update(route="model_error", error=safe_code)
            raise AgentError(safe_code) from None
        except AgentError as error:
            trace["route"] = "cancelled" if error.code == "aborted" else "error"
            raise
        finally:
            if self.dialogue_tasks.get(task_key) is state:
                self.dialogue_tasks.pop(task_key, None)
            trace["total_ms"] = round((time.monotonic() - started) * 1000)
            _LOGGER.info("Assistant dialogue route=%s error=%s provider_ms=%s total_ms=%s",
                         trace["route"], trace.get("error"), trace.get("provider_ms"), trace["total_ms"])

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
        return await self._record_voice_receipt(
            owner, session_id, event_id, history_epoch, receipt_key, payload_digest,
            {"user_text": user_text, "assistant_text": assistant_text},
        )

    async def record_voice_event(self, owner, session_id, event_id, role, text, *,
                                 history_epoch, provider="gemini", correlation=None):
        """Journal one observed provider event without pairing or starting a model."""
        if (not isinstance(owner, str) or not owner or len(owner) > 128
                or (session_id is not None and (not isinstance(session_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id)))
                or not isinstance(event_id, str) or not VOICE_EVENT_ID.fullmatch(event_id)
                or not valid_voice_event_fields(event_id, role, text, provider, correlation)
                or type(history_epoch) is not int or history_epoch < 0):
            raise AgentError("invalid_voice_event")
        _payload, payload_digest, _metadata = encode_voice_event(
            event_id, role, text, provider, correlation)
        receipt_key = json.dumps(
            [owner, history_epoch, VOICE_EVENT_PROTOCOL, event_id],
            ensure_ascii=False, separators=(",", ":"))
        return await self._record_voice_receipt(
            owner, session_id, event_id, history_epoch, receipt_key, payload_digest,
            {"protocol": VOICE_EVENT_PROTOCOL, "role": role, "text": text,
             "provider": provider, "correlation": correlation},
            protocol=VOICE_EVENT_PROTOCOL,
        )

    async def _record_voice_receipt(self, owner, session_id, event_id, history_epoch,
                                    receipt_key, payload_digest, receipt_fields,
                                    protocol=None):
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
                if not isinstance(receipt, dict):
                    raise AgentError("voice_event_conflict")
                expected_fields = receipt_fields
                if protocol is not None and receipt.get("state") == "complete":
                    expected_fields = {
                        key: value for key, value in receipt_fields.items() if key != "text"
                    }
                if (receipt.get("payload_sha256") != payload_digest
                        or (protocol is not None and (
                            receipt.get("protocol") != protocol
                            or any(receipt.get(key) != value
                                   for key, value in expected_fields.items())))):
                    raise AgentError("voice_event_conflict")
                duplicate = True
            else:
                receipt = {
                    "owner": owner, "epoch": history_epoch,
                    "session_id": current_session, "event_id": event_id,
                    "payload_sha256": payload_digest, "state": "pending",
                    **receipt_fields,
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
                if receipt.get("protocol") == VOICE_EVENT_PROTOCOL:
                    try:
                        self._voice_materialization(receipt)
                    except (AgentError, KeyError, TypeError, ValueError):
                        continue
                    projected.append({"role": receipt.get("role"),
                                      "text": receipt.get("text", "")})
                elif receipt.get("protocol") is None:
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
                       and value.get("epoch") == epoch
                       and (value.get("session_id") == session_id
                            or value.get("session_id") is None)]
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
                transcript, digest, metadata, is_event = self._voice_materialization(receipt)
                event_id = receipt["event_id"]
                found = self._has_voice_materialization(
                    messages, event_id, transcript, digest, is_event)
                if not found:
                    await self.runtime.request("POST", f"session/{session_id}/message", {
                        "noReply": True,
                        "parts": [{"type": "text", "text": transcript,
                                   "synthetic": True, "metadata": metadata}],
                    })
                    messages = await self.runtime.request(
                        "GET", f"session/{session_id}/message?limit=2000")
                    found = self._has_voice_materialization(
                        messages, event_id, transcript, digest, is_event)
                    if not found:
                        raise AgentError("runtime_error")
                completed = dict(receipt)
                completed.update(state="complete", session_id=session_id)
                completed.pop("user_text", None)
                completed.pop("assistant_text", None)
                completed.pop("text", None)
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

    @staticmethod
    def _voice_materialization(receipt):
        event_id = receipt.get("event_id")
        if receipt.get("protocol") == VOICE_EVENT_PROTOCOL:
            if not valid_voice_event_fields(
                    event_id, receipt.get("role"), receipt.get("text"),
                    receipt.get("provider"), receipt.get("correlation")):
                raise AgentError("voice_event_conflict")
            transcript, digest, metadata = encode_voice_event(
                event_id, receipt["role"], receipt["text"],
                receipt["provider"], receipt["correlation"])
            if digest != receipt.get("payload_sha256"):
                raise AgentError("voice_event_conflict")
            return transcript, digest, metadata, True
        if receipt.get("protocol") is not None:
            raise AgentError("voice_event_conflict")
        transcript = json.dumps(
            {"user": receipt["user_text"], "assistant": receipt["assistant_text"]},
            ensure_ascii=False, separators=(",", ":"))
        digest = receipt["payload_sha256"]
        return transcript, digest, {
            VOICE_TURN_METADATA_KEY: VOICE_TURN_METADATA_VALUE,
            VOICE_TURN_EVENT_ID_KEY: event_id,
            VOICE_TURN_HASH_KEY: digest,
        }, False

    @staticmethod
    def _has_voice_materialization(messages, event_id, transcript, digest, is_event):
        if not isinstance(messages, list):
            raise AgentError("runtime_error")
        for message in messages:
            if not isinstance(message, dict):
                continue
            parts = message.get("parts", [])
            if not isinstance(parts, list):
                continue
            for part in parts:
                if not isinstance(part, dict):
                    continue
                metadata = part.get("metadata")
                if not isinstance(metadata, dict):
                    continue
                if is_event:
                    if (metadata.get(VOICE_EVENT_METADATA_KEY) != VOICE_EVENT_PROTOCOL
                            or metadata.get(VOICE_EVENT_ID_KEY) != event_id):
                        continue
                    decoded = decode_voice_event_part(part)
                    if (decoded is None or part.get("text") != transcript
                            or metadata.get(VOICE_EVENT_HASH_KEY) != digest):
                        raise AgentError("voice_event_conflict")
                    return True
                if (metadata.get(VOICE_TURN_METADATA_KEY) == VOICE_TURN_METADATA_VALUE
                        and metadata.get(VOICE_TURN_EVENT_ID_KEY) == event_id):
                    if (part.get("synthetic") is not True
                            or metadata.get(VOICE_TURN_HASH_KEY) != digest
                            or part.get("text") != transcript):
                        raise AgentError("voice_event_conflict")
                    return True
        return False

    async def abort(self, owner, channel, session_id, entry_id=None, chat_id=None):
        await self._require_active_admin(owner)
        if not self._mapped_session(owner, channel, session_id, entry_id, chat_id):
            raise AgentError("not_found")
        state = self.dialogue_tasks.get((owner, session_id))
        if state is not None:
            if state["aborted"]:
                return True
            state["aborted"] = True
            # Keep cancellation ownership through executor startup. Once its
            # remote request is active, SetupAgent.run's cancellation handler
            # aborts this exact owner/session and releases the runtime lock.
            state["task"].cancel()
            return True
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
