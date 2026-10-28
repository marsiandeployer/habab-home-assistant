"""Owner-scoped durable feedback receipts and explicit optional delivery."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import uuid
from functools import partial

from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .feedback_transport import (
    EVENT_KEYS,
    TOKEN_PATTERN,
    get_feedback_status,
    send_feedback,
    valid_issue_reference,
)
from .diagnostics import (
    MAX_MESSAGES,
    MAX_TEXT_SCAN_CHARS,
    ProjectionBudget,
    _duration_ms,
    _iso,
    _load_session_messages,
    _mapped_sessions,
    _redact_text,
    _session_status,
    _session_time_bounds,
    _session_data,
    project_messages,
)

MAX_FEEDBACK_CHARS = 2000
MAX_CONTEXT_BYTES = 16 * 1024
MAX_CONTEXT_EVENTS = 100
MAX_RECEIPTS = 100
RETENTION = timedelta(days=30)
STORE_KEY = "habab_home.feedback_receipts"
IDEMPOTENCY_KEY = re.compile(r"[0-9a-fA-F-]{36}\Z")
FEEDBACK_VERSION = 1
DELIVERY_STORE_KEY = "feedback_delivery"
MAX_FEEDBACK_REQUEST_BYTES = 160 * 1024


class FeedbackError(Exception):
    """A user-safe feedback submission failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _active_admin(request):
    user = request.get("hass_user")
    return user if user and user.is_active and user.is_admin else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _is_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _digest(session_id: str, feedback: str, answer_index: int | None = None) -> str:
    identity = {"session_id": session_id, "feedback": feedback, "version": FEEDBACK_VERSION}
    if answer_index is not None:
        identity["answer_index"] = answer_index
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class FeedbackManager:
    """Persist bounded local feedback and purpose-specific delivery settings."""

    def __init__(self, hass):
        self.hass = hass
        self.store = Store(hass, 1, STORE_KEY, private=True)
        self.records: dict[str, dict] = {}
        self.delivery_enabled = False
        self._delivery_token: str | None = None
        self._loaded = False
        self._lock = asyncio.Lock()

    async def _load(self) -> None:
        if self._loaded:
            return
        stored = await self.store.async_load()
        delivery_changed = False
        delivery = stored.get(DELIVERY_STORE_KEY) if isinstance(stored, dict) else None
        if isinstance(stored, dict) and DELIVERY_STORE_KEY in stored and not isinstance(delivery, dict):
            delivery_changed = True
        if isinstance(delivery, dict):
            enabled = delivery.get("enabled")
            token = delivery.get("token")
            if type(enabled) is bool:
                self.delivery_enabled = enabled
            elif "enabled" in delivery:
                delivery_changed = True
            if isinstance(token, str) and TOKEN_PATTERN.fullmatch(token):
                self._delivery_token = token
            elif token is not None:
                delivery_changed = True
            if set(delivery) != {"enabled", "token"}:
                delivery_changed = True
        if not self._delivery_token:
            delivery_changed = delivery_changed or self.delivery_enabled
            self.delivery_enabled = False
        self._register_delivery_token_for_redaction()
        if isinstance(stored, dict) and isinstance(stored.get("records"), dict):
            valid = {
                receipt_id: record
                for receipt_id, record in stored["records"].items()
                if isinstance(receipt_id, str)
                and _is_uuid(receipt_id)
                and isinstance(record, dict)
                and isinstance(record.get("owner_id"), str)
                and isinstance(record.get("idempotency_key"), str)
                and isinstance(record.get("digest"), str)
                and isinstance(record.get("created_at"), str)
                and record.get("state") == "saved_locally"
                and isinstance(record.get("feedback"), str)
                and isinstance(record.get("context"), dict)
            }
            self.records = dict(sorted(valid.items(),
                                       key=lambda item: item[1]["created_at"])[-MAX_RECEIPTS:])
        changed = self._prune()
        if delivery_changed or changed or (isinstance(stored, dict) and isinstance(stored.get("records"), dict)
                                           and len(self.records) != len(stored["records"])):
            await self._save_store()
        self._loaded = True

    async def _ensure_loaded(self) -> None:
        try:
            await self._load()
        except FeedbackError:
            raise
        except Exception as error:
            raise FeedbackError("feedback_unavailable") from error

    def _store_value(self, records: dict | None = None) -> dict:
        return {
            "records": self.records if records is None else records,
            DELIVERY_STORE_KEY: {
                "enabled": self.delivery_enabled,
                "token": self._delivery_token,
            },
        }

    async def _save_store(self, records: dict | None = None) -> None:
        await self.store.async_save(self._store_value(records))

    def _delivery_status(self) -> dict:
        configured = bool(self._delivery_token)
        return {
            "enabled": self.delivery_enabled,
            "credential_configured": configured,
            "ready": self.delivery_enabled and configured,
        }

    def _register_delivery_token_for_redaction(self) -> None:
        if not self._delivery_token:
            return
        setup_agent = self.hass.data.get(DOMAIN, {}).get("setup_agent")
        register = getattr(setup_agent, "_register_runtime_secret", None)
        if callable(register):
            register(self._delivery_token)

    async def async_preload(self) -> bool:
        try:
            async with self._lock:
                await self._ensure_loaded()
        except FeedbackError:
            return False
        return True

    async def async_feedback_delivery_status(self) -> dict:
        async with self._lock:
            await self._ensure_loaded()
            return self._delivery_status()

    async def async_set_feedback_delivery_credential(self, token: str) -> dict:
        if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
            raise FeedbackError("credential_invalid")
        async with self._lock:
            await self._ensure_loaded()
            old = self._delivery_token
            self._delivery_token = token
            try:
                await self._save_store()
            except Exception as error:
                self._delivery_token = old
                raise FeedbackError("feedback_unavailable") from error
            self._register_delivery_token_for_redaction()
            return self._delivery_status()

    async def async_set_feedback_delivery_enabled(self, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise FeedbackError("invalid_enable_state")
        async with self._lock:
            await self._ensure_loaded()
            if enabled and not self._delivery_token:
                raise FeedbackError("credential_required")
            old = self.delivery_enabled
            self.delivery_enabled = enabled
            try:
                await self._save_store()
            except Exception as error:
                self.delivery_enabled = old
                raise FeedbackError("feedback_unavailable") from error
            return self._delivery_status()

    async def async_clear_feedback_delivery_credential(self) -> dict:
        async with self._lock:
            await self._ensure_loaded()
            old_token, old_enabled = self._delivery_token, self.delivery_enabled
            self._delivery_token = None
            self.delivery_enabled = False
            try:
                await self._save_store()
            except Exception as error:
                self._delivery_token, self.delivery_enabled = old_token, old_enabled
                raise FeedbackError("feedback_unavailable") from error
            return self._delivery_status()

    async def _async_deliver_record(self, record: dict, *, retry_ambiguous: bool = False) -> dict:
        """Explicitly send one already-persisted local projection, without exposing its token."""
        async with self._lock:
            await self._ensure_loaded()
            receipt_id = record.get("receipt_id")
            current = self.records.get(receipt_id) if isinstance(receipt_id, str) else None
            if current is None:
                return {"delivery_status": "pending", "delivery_error": "local_receipt_unavailable"}
            token = self._delivery_token if self.delivery_enabled else None
            if current.get("delivery_status") == "saved_on_server":
                if not token:
                    return self._public_delivery(current)
                return await self._async_refresh_server_record(current, token)
            if not token:
                return {"delivery_status": "not_configured"}
            package = {
                "feedback": current["feedback"],
                "context": current["context"],
                "idempotency_key": current["idempotency_key"],
            }
            try:
                result = await self.hass.async_add_executor_job(
                    partial(send_feedback, package, token, retry_ambiguous=retry_ambiguous)
                )
            except Exception:
                result = None
            if result is None or result.status == "pending":
                delivery = {
                    "delivery_status": "pending",
                    "delivery_error": (result.error_code if result else "receiver_unavailable"),
                }
            elif result.status == "saved_on_server":
                delivery = {
                    "delivery_status": "saved_on_server",
                    "server_receipt_id": result.receipt_id,
                    "server_issue_state": result.issue_state,
                    "server_duplicate": result.duplicate,
                }
                if result.issue_state == "created":
                    delivery["server_issue_number"] = result.issue_number
                    delivery["server_issue_url"] = result.issue_url
                if result.error_code:
                    delivery["delivery_error"] = result.error_code
            elif result.status == "rejected":
                delivery = {
                    "delivery_status": "rejected",
                    "delivery_error": result.error_code or "package_invalid",
                }
            else:
                delivery = {"delivery_status": "not_configured"}
            updated_record = {**current, **delivery}
            updated = {**self.records, receipt_id: updated_record}
            try:
                await self._save_store(updated)
            except Exception:
                return {"delivery_status": "pending", "delivery_error": "local_status_unavailable"}
            self.records = updated
            if result.status == "saved_on_server" and result.issue_state == "created":
                return await self._async_refresh_server_record(updated_record, token)
            return self._public_delivery(updated_record)

    async def _async_refresh_server_record(self, record: dict, token: str) -> dict:
        """Refresh issue metadata for this already accepted server receipt."""
        server_receipt_id = record.get("server_receipt_id")
        try:
            result = await self.hass.async_add_executor_job(
                partial(get_feedback_status, server_receipt_id, token)
            )
        except Exception:
            result = None
        if result is None or result.status != "saved_on_server":
            error = result.error_code if result else "receiver_unavailable"
            return {
                "delivery_status": "pending",
                "delivery_error": error or "receiver_unavailable",
                "server_receipt_id": server_receipt_id,
            }

        refreshed = {
            key: value for key, value in record.items()
            if key not in {
                "server_issue_state", "server_issue_number", "server_issue_url", "delivery_error",
            }
        }
        refreshed.update({
            "delivery_status": "saved_on_server",
            "server_receipt_id": result.receipt_id,
            "server_issue_state": result.issue_state,
            "server_duplicate": result.duplicate,
        })
        if result.issue_state == "created":
            refreshed["server_issue_number"] = result.issue_number
            refreshed["server_issue_url"] = result.issue_url
        if result.error_code:
            refreshed["delivery_error"] = result.error_code
        updated = {**self.records, record["receipt_id"]: refreshed}
        try:
            await self._save_store(updated)
        except Exception:
            return {
                "delivery_status": "pending",
                "delivery_error": "local_status_unavailable",
                "server_receipt_id": server_receipt_id,
            }
        self.records = updated
        return self._public_delivery(refreshed)

    @staticmethod
    def _public_delivery(record: dict) -> dict:
        fields = (
            "delivery_status", "delivery_error", "server_receipt_id",
            "server_issue_state", "server_issue_number", "server_issue_url", "server_duplicate",
        )
        public = {key: record[key] for key in fields if key in record and record[key] is not None}
        if public.get("server_issue_state") == "created" and not valid_issue_reference(
            public.get("server_issue_number"), public.get("server_issue_url"),
        ):
            public["server_issue_state"] = "unknown"
            public["delivery_error"] = "issue_reference_unavailable"
        if public.get("server_issue_state") != "created":
            public.pop("server_issue_number", None)
            public.pop("server_issue_url", None)
        if public.get("delivery_status") != "saved_on_server":
            public.pop("server_issue_state", None)
            public.pop("server_issue_number", None)
            public.pop("server_issue_url", None)
        return public

    def _prune(self) -> bool:
        cutoff = _now() - RETENTION
        fresh = {}
        for receipt_id, record in self.records.items():
            try:
                created = datetime.fromisoformat(record["created_at"].replace("Z", "+00:00"))
            except (KeyError, TypeError, ValueError):
                continue
            if created.tzinfo is not None and created >= cutoff:
                fresh[receipt_id] = record
        changed = len(fresh) != len(self.records)
        self.records = fresh
        return changed

    async def _prune_and_save(self) -> None:
        previous = self.records
        if self._prune():
            try:
                await self._save_store()
            except Exception:
                self.records = previous
                raise

    async def idempotent_record(self, owner_id: str, key: str, digest: str) -> dict | None:
        async with self._lock:
            await self._load()
            await self._prune_and_save()
            existing = next((record for record in self.records.values()
                             if record["owner_id"] == owner_id
                             and record["idempotency_key"] == key), None)
            if existing is None:
                return None
            if existing["digest"] != digest:
                raise FeedbackError("idempotency_conflict")
            return existing

    async def async_save(self, *, owner_id: str, key: str, digest: str,
                         feedback: str, context: dict,
                         trace_status: str | None = None) -> tuple[dict, bool]:
        async with self._lock:
            await self._load()
            await self._prune_and_save()
            existing = next((record for record in self.records.values()
                             if record["owner_id"] == owner_id
                             and record["idempotency_key"] == key), None)
            if existing is not None:
                if existing["digest"] != digest:
                    raise FeedbackError("idempotency_conflict")
                return existing, False
            if len(self.records) >= MAX_RECEIPTS:
                raise FeedbackError("storage_limit")
            now = _now()
            receipt_id = str(uuid.uuid4())
            record = {
                "receipt_id": receipt_id,
                "owner_id": owner_id,
                "idempotency_key": key,
                "digest": digest,
                "created_at": _iso(now),
                "state": "saved_locally",
                "issue_state": "not_configured",
                "feedback": feedback,
                "context": context,
            }
            if trace_status in {"available", "truncated", "unavailable"}:
                record["trace_status"] = trace_status
            updated = {**self.records, receipt_id: record}
            await self._save_store(updated)
            self.records = updated
            return record, True

    async def async_receipt(self, owner_id: str, receipt_id: str) -> dict | None:
        async with self._lock:
            await self._load()
            await self._prune_and_save()
            record = self.records.get(receipt_id)
            if record is None or record["owner_id"] != owner_id:
                return None
            return record


def _public_receipt(record: dict, duplicate: bool = False) -> dict:
    receipt = {
        "receipt_id": record["receipt_id"],
        "state": record["state"],
        "issue_state": record["issue_state"],
        "created_at": record["created_at"],
        "duplicate": duplicate,
    }
    if record.get("trace_status") in {"available", "truncated", "unavailable"}:
        receipt["trace_status"] = record["trace_status"]
    return receipt


def _selected_runtime_answer(runtime, messages: list, answer_index: int,
                             answer_text: str) -> tuple[str, int] | None:
    """Map a clicked visible answer back to its raw OpenCode message, if recorded."""
    visible = runtime.visible(messages)
    if (not isinstance(visible, list) or answer_index >= len(visible)
            or not isinstance(visible[answer_index], dict)
            or visible[answer_index].get("role") != "assistant"
            or visible[answer_index].get("text") != answer_text):
        return None
    selected = visible[answer_index]
    for raw_index in range(len(messages)):
        prefix = runtime.visible(messages[:raw_index + 1])
        if (isinstance(prefix, list) and answer_index < len(prefix)
                and prefix[answer_index] == selected):
            return selected["text"], raw_index
    return None


def _selected_runtime_prompt(runtime, messages: list, answer_index: int) -> str | None:
    """Return the nearest visible user message before this answer."""
    visible = runtime.visible(messages)
    if not isinstance(visible, list) or not 0 <= answer_index <= len(visible):
        return None
    for message in reversed(visible[:answer_index]):
        if (isinstance(message, dict) and message.get("role") == "user"
                and isinstance(message.get("text"), str) and message["text"].strip()):
            return message["text"]
    return None


def _bounded_redacted_text(runtime, value: str, byte_limit: int) -> tuple[str, bool]:
    text = _redact_text(runtime, value, limit=MAX_TEXT_SCAN_CHARS)

    def encoded_size(candidate: str) -> int:
        return len(json.dumps(candidate, ensure_ascii=False).encode("utf-8"))

    if encoded_size(text) <= byte_limit:
        return text, False
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if encoded_size(text[:middle]) <= byte_limit:
            low = middle
        else:
            high = middle - 1
    return text[:low], True


class FeedbackSubmitView(HomeAssistantView):
    url = "/api/habab_home/feedback"
    name = "api:habab_home:feedback:submit"
    requires_auth = True

    async def post(self, request):
        owner = _active_admin(request)
        if owner is None:
            return self.json_message("not_allowed", status_code=403)
        if getattr(request, "content_length", None) is not None and request.content_length > MAX_FEEDBACK_REQUEST_BYTES:
            return self.json_message("invalid_feedback", status_code=413)
        try:
            body = await request.json()
            legacy_fields = {"session_id", "feedback", "consent", "idempotency_key"}
            answer_fields = legacy_fields | {"answer_index", "answer_text", "prompt_text"}
            if not isinstance(body, dict) or set(body) not in (legacy_fields, answer_fields):
                raise ValueError("invalid_request")
            session_id = body["session_id"]
            feedback = body["feedback"]
            key = body["idempotency_key"]
            answer_index = body.get("answer_index")
            answer_text = body.get("answer_text")
            prompt_text = body.get("prompt_text")
            if (not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id)
                    or not isinstance(feedback, str) or not 1 <= len(feedback.strip()) <= MAX_FEEDBACK_CHARS
                    or body["consent"] is not True
                    or not isinstance(key, str) or not IDEMPOTENCY_KEY.fullmatch(key)
                    or ("answer_index" in body and (
                        type(answer_index) is not int or not 0 <= answer_index < 4096
                        or not isinstance(answer_text, str)
                        or not 1 <= len(answer_text.strip()) <= MAX_TEXT_SCAN_CHARS
                        or not isinstance(prompt_text, str) or len(prompt_text) > 4000
                    ))):
                raise ValueError("invalid_request")
            key = str(uuid.UUID(key))
        except (ValueError, TypeError, KeyError):
            return self.json_message("invalid_feedback", status_code=400)

        hass = request.app["hass"]
        domain_data = hass.data.get(DOMAIN, {})
        assistant = domain_data.get("assistant")
        manager = domain_data.get("feedback")
        if assistant is None or not isinstance(manager, FeedbackManager):
            return self.json_message("feedback_unavailable", status_code=503)
        mappings = [mapping for mapping in _mapped_sessions(assistant, owner.id)
                    if mapping["session_id"] == session_id]
        if len(mappings) != 1:
            return self.json_message("session_not_found", status_code=404)

        normalized_feedback = feedback.strip()
        digest = _digest(session_id, normalized_feedback, answer_index)
        try:
            existing = await manager.idempotent_record(owner.id, key, digest)
            if existing is not None:
                if answer_index is not None:
                    delivery = await manager._async_deliver_record(existing)
                    response = self.json({**_public_receipt(existing, duplicate=True), **delivery})
                    response.headers["Cache-Control"] = "no-store"
                    return response
                response = self.json(_public_receipt(existing, duplicate=True))
                response.headers["Cache-Control"] = "no-store"
                return response
            runtime = assistant.runtime
            safe_feedback = _redact_text(runtime, normalized_feedback, limit=MAX_FEEDBACK_CHARS)
            if answer_index is None:
                budget = ProjectionBudget(max_bytes=MAX_CONTEXT_BYTES, max_events=MAX_CONTEXT_EVENTS)
                session, events, _messages = await _session_data(
                    hass, owner.id, mappings[0], budget=budget)
            else:
                session = None
                events = []
                _messages = []
                try:
                    runtime, _messages, history_truncated = await _load_session_messages(
                        hass, owner.id, mappings[0],
                    )
                    visible = runtime.visible(_messages)
                    if not isinstance(visible, list):
                        visible = []
                    created_at, updated_at = _session_time_bounds(_messages)
                    status, _error = _session_status(runtime, owner.id, session_id, _messages)
                    session = {
                        "session_id": session_id,
                        "channel": mappings[0]["channel"],
                        "modes": [mappings[0]["channel"]],
                        "status": status,
                        "provider_models": [],
                        "created_at": _iso(created_at),
                        "updated_at": _iso(updated_at),
                        "processing_duration_ms": sum(
                            _duration_ms(message) or 0 for message in _messages
                            if isinstance(message, dict)
                            and isinstance(message.get("info"), dict)
                            and message["info"].get("role") == "assistant"
                        ) or None,
                        "message_count": min(len(visible), MAX_MESSAGES),
                        "events_truncated": history_truncated,
                    }
                except Exception:
                    # A click can still report a visible native Assist answer after
                    # OpenCode history is unavailable. The submitted text is redacted
                    # below and the package explicitly marks the trace unavailable.
                    session = {
                        "session_id": session_id,
                        "channel": mappings[0]["channel"],
                        "modes": [mappings[0]["channel"]],
                        "status": "unknown",
                        "provider_models": [],
                        "created_at": None,
                        "updated_at": None,
                        "processing_duration_ms": None,
                        "message_count": 0,
                        "events_truncated": False,
                    }
            context = {
                "channel": session["channel"],
                "session": {
                    "provider_models": session["provider_models"],
                    "created_at": session["created_at"],
                    "updated_at": session["updated_at"],
                    "status": session["status"],
                    "processing_duration_ms": session["processing_duration_ms"],
                    "message_count": session["message_count"],
                    "events_truncated": session["events_truncated"],
                },
                "events": events,
                "source": {
                    "kind": "owner_mapped_current_session",
                    "history_message_limit": MAX_MESSAGES,
                    "truncated": session["events_truncated"],
                },
            }
            if answer_index is not None:
                selected_answer = _selected_runtime_answer(runtime, _messages, answer_index, answer_text)
                safe_answer, answer_truncated = _bounded_redacted_text(
                    runtime, selected_answer[0] if selected_answer else answer_text, 5000,
                )
                safe_prompt, prompt_truncated = _bounded_redacted_text(runtime, prompt_text, 1500)
                if selected_answer is not None:
                    _canonical_answer, answer_raw_index = selected_answer
                    canonical_prompt = _selected_runtime_prompt(runtime, _messages, answer_index)
                    prompt_source = canonical_prompt if canonical_prompt is not None else prompt_text
                    safe_prompt, prompt_truncated = _bounded_redacted_text(
                        runtime, prompt_source, 1500,
                    )
                    start_index = answer_raw_index
                    turn_has_user = False
                    for raw_index in range(answer_raw_index, -1, -1):
                        info = _messages[raw_index].get("info") if isinstance(_messages[raw_index], dict) else None
                        if isinstance(info, dict) and info.get("role") == "user":
                            start_index = raw_index
                            turn_has_user = True
                            break
                    prompt_event = {
                        "id": "selected.prompt", "at": None, "channel": mappings[0]["channel"],
                        "stage": "conversation_message", "kind": "user_message", "role": "user",
                        "text": safe_prompt or None,
                        "confirmation": "runtime_message_recorded" if canonical_prompt is not None else "unconfirmed",
                        "available": bool(safe_prompt),
                    }
                    answer_event = {
                        "id": "selected.answer", "at": None, "channel": mappings[0]["channel"],
                        "stage": "model_text", "kind": "assistant_text", "role": "assistant",
                        "text": safe_answer, "confirmation": "runtime_answer_recorded", "available": True,
                    }
                    context["events"] = [prompt_event, answer_event]
                    base_context_bytes = len(json.dumps(
                        context, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
                    ).encode("utf-8"))
                    reserved_context_bytes = max(
                        0, MAX_CONTEXT_BYTES - base_context_bytes - 256,
                    )
                    answer_budget = ProjectionBudget(
                        max_bytes=reserved_context_bytes,
                        max_events=max(0, MAX_CONTEXT_EVENTS - 2),
                    )
                    turn_messages = []
                    for message in _messages[start_index:answer_raw_index + 1]:
                        if (isinstance(message, dict)
                                and isinstance(message.get("info"), dict)
                                and message["info"].get("role") == "assistant"
                                and isinstance(message.get("parts"), list)):
                            message = {
                                **message,
                                "parts": [part for part in message["parts"]
                                          if not isinstance(part, dict) or part.get("type") != "text"],
                            }
                        turn_messages.append(message)
                    scoped_events = project_messages(
                        runtime, turn_messages,
                        mappings[0]["channel"], budget=answer_budget,
                    )
                    trace_events = [
                        {key: value for key, value in event.items() if key in EVENT_KEYS}
                        for event in scoped_events
                        if not (event.get("role") == "assistant" and event.get("kind") == "assistant_text")
                        and not (event.get("role") == "user" and event.get("kind") == "user_message")
                    ]
                    context["events"] = [prompt_event, *trace_events, answer_event]
                    serialized_context_bytes = len(json.dumps(
                        context, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
                    ).encode("utf-8"))
                    while serialized_context_bytes > MAX_CONTEXT_BYTES and trace_events:
                        trace_events.pop()
                        answer_budget.mark_truncated()
                        context["events"] = [prompt_event, *trace_events, answer_event]
                        serialized_context_bytes = len(json.dumps(
                            context, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
                        ).encode("utf-8"))
                    truncated = (
                        answer_budget.truncation_count > 0 or answer_truncated or prompt_truncated
                        or canonical_prompt is None
                        or (session["events_truncated"] and not turn_has_user)
                    )
                    context["session"]["events_truncated"] = truncated
                    context["source"]["truncated"] = truncated
                    trace_status = "truncated" if truncated else "available"
                else:
                    # Some visible replies (notably native Assist) are not part of
                    # the OpenCode runtime history. Keep only the clicked exchange.
                    context["events"] = ([{
                        "id": "selected.prompt", "at": None, "channel": "voice",
                        "stage": "conversation_message", "kind": "user_message",
                        "role": "user", "text": safe_prompt or None,
                        "confirmation": "unconfirmed", "available": True,
                    }] if safe_prompt else []) + [{
                        "id": "selected.answer", "at": None, "channel": "voice",
                        "stage": "model_text", "kind": "assistant_text",
                        "role": "assistant", "text": safe_answer,
                        "confirmation": "not_recorded_by_habab", "available": True,
                    }, {
                        "id": "selected.trace", "at": None, "channel": "voice",
                        "stage": "trace_unavailable", "kind": "stage_unavailable",
                        "role": "assistant", "text": None,
                        "confirmation": "not_recorded_by_habab", "available": False,
                        "reason": "trace_not_recorded_by_runtime",
                    }]
                    trace_status = "unavailable"
                    context["source"]["truncated"] = answer_truncated or prompt_truncated
                    context["session"]["events_truncated"] = context["source"]["truncated"]
            record, _created = await manager.async_save(
                owner_id=owner.id, key=key, digest=digest,
                feedback=safe_feedback, context=context,
                trace_status=trace_status if answer_index is not None else None)
            if answer_index is not None:
                delivery = await manager._async_deliver_record(record)
                response = self.json({**_public_receipt(record), **delivery})
                response.headers["Cache-Control"] = "no-store"
                return response
        except FeedbackError as error:
            status = 409 if error.code == "idempotency_conflict" else 507
            return self.json_message(error.code, status_code=status)
        except Exception:
            return self.json_message("feedback_unavailable", status_code=503)
        response = self.json(_public_receipt(record))
        response.headers["Cache-Control"] = "no-store"
        return response


class FeedbackReceiptView(HomeAssistantView):
    url = "/api/habab_home/feedback/{receipt_id}"
    name = "api:habab_home:feedback:receipt"
    requires_auth = True

    async def get(self, request, receipt_id):
        owner = _active_admin(request)
        if owner is None:
            return self.json_message("not_allowed", status_code=403)
        try:
            receipt_id = str(uuid.UUID(receipt_id))
        except (ValueError, TypeError, AttributeError):
            return self.json_message("not_found", status_code=404)
        manager = request.app["hass"].data.get(DOMAIN, {}).get("feedback")
        if not isinstance(manager, FeedbackManager):
            return self.json_message("not_found", status_code=404)
        try:
            record = await manager.async_receipt(owner.id, receipt_id)
        except Exception:
            return self.json_message("feedback_unavailable", status_code=503)
        if record is None:
            return self.json_message("not_found", status_code=404)
        response = self.json(_public_receipt(record))
        response.headers["Cache-Control"] = "no-store"
        return response


def async_register_views(hass):
    """Register authenticated local receipt routes and their private store."""
    manager = FeedbackManager(hass)
    hass.data[DOMAIN]["feedback"] = manager
    create_task = getattr(hass, "async_create_task", None)
    if callable(create_task):
        create_task(manager.async_preload())
    hass.http.register_view(FeedbackSubmitView())
    hass.http.register_view(FeedbackReceiptView())
