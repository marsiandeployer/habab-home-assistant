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
from .feedback_transport import TOKEN_PATTERN, send_feedback
from .diagnostics import (
    MAX_MESSAGES,
    ProjectionBudget,
    _mapped_sessions,
    _redact_text,
    _session_data,
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


def _digest(session_id: str, feedback: str) -> str:
    canonical = json.dumps(
        {"session_id": session_id, "feedback": feedback, "version": FEEDBACK_VERSION},
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
            if current.get("delivery_status") == "saved_on_server":
                return self._public_delivery(current)
            token = self._delivery_token if self.delivery_enabled else None
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
            return self._public_delivery(updated_record)

    @staticmethod
    def _public_delivery(record: dict) -> dict:
        fields = (
            "delivery_status", "delivery_error", "server_receipt_id",
            "server_issue_state", "server_duplicate",
        )
        return {key: record[key] for key in fields if key in record and record[key] is not None}

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
                         feedback: str, context: dict) -> tuple[dict, bool]:
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
    return {
        "receipt_id": record["receipt_id"],
        "state": record["state"],
        "issue_state": record["issue_state"],
        "created_at": record["created_at"],
        "duplicate": duplicate,
    }


class FeedbackSubmitView(HomeAssistantView):
    url = "/api/habab_home/feedback"
    name = "api:habab_home:feedback:submit"
    requires_auth = True

    async def post(self, request):
        owner = _active_admin(request)
        if owner is None:
            return self.json_message("not_allowed", status_code=403)
        if getattr(request, "content_length", None) is not None and request.content_length > 8192:
            return self.json_message("invalid_feedback", status_code=413)
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {
                    "session_id", "feedback", "consent", "idempotency_key"}:
                raise ValueError("invalid_request")
            session_id = body["session_id"]
            feedback = body["feedback"]
            key = body["idempotency_key"]
            if (not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id)
                    or not isinstance(feedback, str) or not 1 <= len(feedback.strip()) <= MAX_FEEDBACK_CHARS
                    or body["consent"] is not True
                    or not isinstance(key, str) or not IDEMPOTENCY_KEY.fullmatch(key)):
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
        digest = _digest(session_id, normalized_feedback)
        try:
            existing = await manager.idempotent_record(owner.id, key, digest)
            if existing is not None:
                response = self.json(_public_receipt(existing, duplicate=True))
                response.headers["Cache-Control"] = "no-store"
                return response
            budget = ProjectionBudget(max_bytes=MAX_CONTEXT_BYTES, max_events=MAX_CONTEXT_EVENTS)
            session, events, _messages = await _session_data(
                hass, owner.id, mappings[0], budget=budget)
            runtime = assistant.runtime
            safe_feedback = _redact_text(runtime, normalized_feedback, limit=MAX_FEEDBACK_CHARS)
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
            record, _created = await manager.async_save(
                owner_id=owner.id, key=key, digest=digest,
                feedback=safe_feedback, context=context)
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
