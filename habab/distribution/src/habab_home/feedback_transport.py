"""Optional transport for explicitly requested, locally saved feedback.

The shared feedback action invokes this sender through the Home Assistant
executor only when remote delivery is requested. It accepts a purpose-specific
installation token in memory; absent or disabled credentials cause no request.
A durable receiver receipt reports ``pending_issue`` and does not imply that a
GitHub issue was created.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import math
import re
import socket
import ssl
from typing import Any
from urllib.parse import urlsplit
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)


RECEIVER_URL = "https://habab.ru/api/v1/feedback"
REQUEST_TIMEOUT_SECONDS = 5
MAX_REQUEST_BYTES = 24 * 1024
MAX_RESPONSE_BYTES = 4096
MAX_FEEDBACK_CHARS = 2000
MAX_CONTEXT_BYTES = 16 * 1024
MAX_CONTEXT_EVENTS = 100
EVENT_KEYS = {
    "at", "available", "channel", "confirmation", "duration_ms", "exit_code",
    "id", "input", "kind", "model", "output", "provider", "reason", "role",
    "stage", "status", "text", "tool",
}
TEXT_EVENT_FIELDS = {
    "at", "channel", "confirmation", "id", "kind", "model", "provider",
    "reason", "role", "stage", "status", "tool",
}
SENSITIVE_KEY_PARTS = {
    "accesskey", "apikey", "authorization", "cookie", "credential", "password",
    "refreshkey", "secret", "sessionid", "token",
}
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
CONSENT = {
    "explicit": True,
    "scope": "feedback_and_bounded_context",
    "version": 1,
}
RECEIPT_ISSUE_STATES = {"pending_issue", "sending", "created", "unknown"}


@dataclass(frozen=True)
class FeedbackDeliveryResult:
    """Safe result summary; never contains credentials or submitted content."""

    status: str
    receipt_id: str | None = None
    issue_state: str | None = None
    issue_number: int | None = None
    issue_url: str | None = None
    duplicate: bool | None = None
    attempts: int = 0
    error_code: str | None = None


@dataclass(frozen=True)
class _Attempt:
    receipt: dict[str, Any] | None = None
    error_code: str | None = None
    ambiguous: bool = False


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _build_opener():
    # Do not inherit arbitrary proxy configuration or follow a redirect to a
    # different host with the per-installation bearer credential.
    return build_opener(
        ProxyHandler({}),
        HTTPSHandler(context=ssl.create_default_context()),
        _NoRedirect(),
    )


def _safe_json_tree(value: Any, *, depth: int = 0) -> bool:
    """Mirror the receiver's bounded JSON and sensitive-key checks."""
    if depth > 5:
        return False
    if value is None or type(value) in (bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if isinstance(value, str):
        return len(value) <= 8192
    if isinstance(value, list):
        return len(value) <= 50 and all(_safe_json_tree(item, depth=depth + 1) for item in value)
    if isinstance(value, dict):
        if len(value) > 50:
            return False
        for key, item in value.items():
            if not isinstance(key, str) or not 1 <= len(key) <= 100:
                return False
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if any(part in normalized for part in SENSITIVE_KEY_PARTS):
                return False
            if normalized in {"owner", "ownerid", "sessionid", "audio", "homeconfig"}:
                return False
            if not _safe_json_tree(item, depth=depth + 1):
                return False
        return True
    return False


def _timestamp_or_none(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, str) or len(value) > 64:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _context_bytes(context: Any) -> bytes | None:
    if not isinstance(context, dict) or set(context) != {"channel", "session", "events", "source"}:
        return None
    if not isinstance(context.get("channel"), str) or context["channel"] not in {"web", "telegram"}:
        return None
    session = context.get("session")
    if not isinstance(session, dict) or set(session) != {
        "created_at", "events_truncated", "message_count", "processing_duration_ms",
        "provider_models", "status", "updated_at",
    }:
        return None
    source = context.get("source")
    if not isinstance(source, dict) or set(source) != {
        "history_message_limit", "kind", "truncated",
    }:
        return None
    if (
        type(session.get("events_truncated")) is not bool
        or type(session.get("message_count")) is not int
        or not 0 <= session["message_count"] <= 60
        or not _timestamp_or_none(session.get("created_at"))
        or not _timestamp_or_none(session.get("updated_at"))
        or type(source.get("truncated")) is not bool
        or type(source.get("history_message_limit")) is not int
        or not 1 <= source["history_message_limit"] <= 60
        or source.get("kind") != "owner_mapped_current_session"
        or not isinstance(session.get("status"), str)
        or len(session["status"]) > 40
        or (
            session.get("processing_duration_ms") is not None
            and (
                type(session["processing_duration_ms"]) is not int
                or not 0 <= session["processing_duration_ms"] <= 86_400_000
            )
        )
    ):
        return None
    models = session.get("provider_models")
    events = context.get("events")
    if (
        not isinstance(models, list)
        or len(models) > 8
        or not isinstance(events, list)
        or len(events) > MAX_CONTEXT_EVENTS
    ):
        return None
    if any(
        not isinstance(model, dict)
        or set(model) != {"model", "provider"}
        or any(
            value is not None and (not isinstance(value, str) or len(value) > 160)
            for value in model.values()
        )
        for model in models
    ):
        return None
    for event in events:
        if not isinstance(event, dict) or not set(event).issubset(EVENT_KEYS):
            return None
        for key, value in event.items():
            if key in TEXT_EVENT_FIELDS and value is not None:
                if not isinstance(value, str) or len(value) > 12_000:
                    return None
            elif key in {"duration_ms", "exit_code"} and value is not None:
                if type(value) is not int or not -255 <= value <= 86_400_000:
                    return None
            elif key == "available" and value is not None and type(value) is not bool:
                return None
            elif key in {"input", "output"} and not _safe_json_tree(value):
                return None
    try:
        encoded = json.dumps(
            context, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        return None
    return encoded if len(encoded) <= MAX_CONTEXT_BYTES else None


def _request_body(package: Any) -> tuple[bytes, str] | None:
    if not isinstance(package, dict) or set(package) != {"feedback", "context", "idempotency_key"}:
        return None
    feedback = package.get("feedback")
    key = package.get("idempotency_key")
    context = package.get("context")
    if not isinstance(feedback, str) or not 1 <= len(feedback.strip()) <= MAX_FEEDBACK_CHARS:
        return None
    if not isinstance(key, str):
        return None
    try:
        parsed_key = str(uuid.UUID(key))
    except (ValueError, TypeError, AttributeError):
        return None
    if parsed_key != key.lower():
        return None
    if _context_bytes(context) is None:
        return None
    body = {
        "schema_version": 1,
        "consent": CONSENT,
        "channel": context["channel"],
        "feedback": feedback.strip(),
        "context": context,
    }
    try:
        encoded = json.dumps(
            body, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        return None
    if len(encoded) > MAX_REQUEST_BYTES:
        return None
    return encoded, parsed_key


def _valid_receipt(status_code: int, headers: Any, raw: bytes) -> dict[str, Any] | None:
    if status_code != 200 or len(raw) > MAX_RESPONSE_BYTES:
        return None
    content_type = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    cache_control = {part.strip().lower() for part in headers.get("Cache-Control", "").split(",")}
    if content_type != "application/json" or "no-store" not in cache_control:
        return None
    try:
        receipt = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    base_fields = {"receipt_id", "state", "issue_state", "created_at", "duplicate"}
    issue_fields = {"issue_number", "issue_url"}
    if not isinstance(receipt, dict) or frozenset(receipt) not in {
        frozenset(base_fields), frozenset(base_fields | issue_fields),
    }:
        return None
    receipt_id = receipt.get("receipt_id")
    created_at = receipt.get("created_at")
    if not isinstance(receipt_id, str) or type(receipt.get("duplicate")) is not bool:
        return None
    try:
        if str(uuid.UUID(receipt_id)) != receipt_id.lower():
            return None
        parsed_date = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None
    if (
        parsed_date.tzinfo is None
        or receipt.get("state") != "saved"
        or not isinstance(receipt.get("issue_state"), str)
        or receipt["issue_state"] not in RECEIPT_ISSUE_STATES
    ):
        return None
    if receipt["issue_state"] == "created":
        if set(receipt) == base_fields:
            # Older receivers report `created` without the persisted link. Keep
            # the durable save, but do not surface an unverified creation claim.
            receipt = {
                **receipt,
                "issue_state": "unknown",
                "status_error": "issue_reference_unavailable",
            }
        elif not valid_issue_reference(receipt.get("issue_number"), receipt.get("issue_url")):
            return None
    elif set(receipt) != base_fields:
        return None
    return receipt


def valid_issue_reference(issue_number: Any, issue_url: Any) -> bool:
    if type(issue_number) is not int or issue_number < 1:
        return False
    if not isinstance(issue_url, str) or len(issue_url) > 300:
        return False
    try:
        parts = urlsplit(issue_url)
        segments = parts.path.split("/")
        if (
            parts.scheme != "https"
            or parts.netloc != "github.com"
            or parts.query
            or parts.fragment
            or parts.username is not None
            or parts.password is not None
            or parts.port is not None
            or len(segments) != 5
            or segments[0] != ""
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", segments[1])
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", segments[2])
            or segments[3] != "issues"
            or segments[4] != str(issue_number)
        ):
            return False
        return issue_url == f"https://github.com/{segments[1]}/{segments[2]}/issues/{issue_number}"
    except ValueError:
        return False


def _status_url(receipt_id: str) -> str | None:
    if not isinstance(receipt_id, str):
        return None
    try:
        parsed = uuid.UUID(receipt_id)
    except (ValueError, TypeError, AttributeError):
        return None
    if str(parsed) != receipt_id.lower():
        return None
    return f"{RECEIVER_URL}/{parsed}"


def _status_once(opener: Any, receipt_id: str, token: str) -> _Attempt:
    url = _status_url(receipt_id)
    if url is None:
        return _Attempt(error_code="invalid_receipt_id")
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Cache-Control": "no-store",
        },
        method="GET",
    )
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            status_code = response.status
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            receipt = _valid_receipt(status_code, response.headers, raw)
            if receipt is None or receipt.get("receipt_id") != str(uuid.UUID(receipt_id)):
                return _Attempt(error_code="invalid_receipt")
            return _Attempt(receipt=receipt)
    except HTTPError as error:
        status_code = error.code
        error.close()
        if 300 <= status_code < 400:
            return _Attempt(error_code="redirect_rejected")
        if status_code == 401:
            return _Attempt(error_code="unauthorized")
        if status_code == 404:
            return _Attempt(error_code="receipt_not_found")
        if status_code >= 500:
            return _Attempt(error_code="receiver_unavailable", ambiguous=True)
        return _Attempt(error_code=f"receiver_http_{status_code}")
    except (TimeoutError, socket.timeout):
        return _Attempt(error_code="timeout", ambiguous=True)
    except URLError as error:
        if isinstance(error.reason, (TimeoutError, socket.timeout)):
            return _Attempt(error_code="timeout", ambiguous=True)
        return _Attempt(error_code="network_unavailable", ambiguous=True)
    except OSError:
        return _Attempt(error_code="network_unavailable", ambiguous=True)


def get_feedback_status(
    receipt_id: str,
    installation_token: str | None,
    *,
    _opener: Any = None,
) -> FeedbackDeliveryResult:
    """Read one existing receipt from the fixed authenticated receiver."""
    if not isinstance(installation_token, str) or not installation_token:
        return FeedbackDeliveryResult(status="not_configured")
    if not TOKEN_PATTERN.fullmatch(installation_token):
        return FeedbackDeliveryResult(status="rejected", error_code="credential_invalid")
    if _status_url(receipt_id) is None:
        return FeedbackDeliveryResult(status="rejected", error_code="invalid_receipt_id")
    opener = _opener if _opener is not None else _build_opener()
    attempt = _status_once(opener, receipt_id, installation_token)
    if attempt.receipt is None:
        return FeedbackDeliveryResult(
            status="pending", receipt_id=receipt_id,
            attempts=1, error_code=attempt.error_code or "receiver_unavailable",
        )
    receipt = attempt.receipt
    return FeedbackDeliveryResult(
        status="saved_on_server",
        receipt_id=receipt["receipt_id"],
        issue_state=receipt["issue_state"],
        issue_number=receipt.get("issue_number"),
        issue_url=receipt.get("issue_url"),
        duplicate=receipt["duplicate"],
        attempts=1,
        error_code=receipt.get("status_error"),
    )


def _post_once(opener: Any, body: bytes, idempotency_key: str, token: str) -> _Attempt:
    request = Request(
        RECEIVER_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": idempotency_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            status_code = response.status
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            receipt = _valid_receipt(status_code, response.headers, raw)
            if receipt is not None:
                return _Attempt(receipt=receipt)
            return _Attempt(error_code="invalid_receipt", ambiguous=True)
    except HTTPError as error:
        status_code = error.code
        error.close()
        if status_code >= 500:
            return _Attempt(error_code="receiver_unavailable", ambiguous=True)
        if 300 <= status_code < 400:
            return _Attempt(error_code="redirect_rejected")
        if status_code == 401:
            return _Attempt(error_code="unauthorized")
        if status_code == 409:
            return _Attempt(error_code="idempotency_conflict")
        return _Attempt(error_code=f"receiver_http_{status_code}")
    except (TimeoutError, socket.timeout):
        return _Attempt(error_code="timeout", ambiguous=True)
    except URLError as error:
        if isinstance(error.reason, (TimeoutError, socket.timeout)):
            return _Attempt(error_code="timeout", ambiguous=True)
        return _Attempt(error_code="network_unavailable", ambiguous=True)
    except OSError:
        return _Attempt(error_code="network_unavailable", ambiguous=True)


def send_feedback(
    package: dict[str, Any],
    installation_token: str | None,
    *,
    retry_ambiguous: bool = False,
    _opener: Any = None,
) -> FeedbackDeliveryResult:
    """Send one local redacted package to the fixed Habab receiver.

    Ambiguous outcomes are never called successful without a validated durable
    receipt. Callers may explicitly opt into exactly one same-key retry to
    recover a committed request whose response was lost.
    """
    if not isinstance(installation_token, str) or not installation_token:
        return FeedbackDeliveryResult(status="not_configured")
    if not TOKEN_PATTERN.fullmatch(installation_token):
        return FeedbackDeliveryResult(status="rejected", error_code="credential_invalid")
    if type(retry_ambiguous) is not bool:
        return FeedbackDeliveryResult(status="rejected", error_code="invalid_retry_option")
    encoded = _request_body(package)
    if encoded is None:
        return FeedbackDeliveryResult(status="rejected", error_code="package_invalid")
    body, key = encoded
    opener = _opener if _opener is not None else _build_opener()
    max_attempts = 2 if retry_ambiguous else 1
    for attempt_number in range(1, max_attempts + 1):
        attempt = _post_once(opener, body, key, installation_token)
        if attempt.receipt is not None:
            receipt = attempt.receipt
            return FeedbackDeliveryResult(
                status="saved_on_server",
                receipt_id=receipt["receipt_id"],
                issue_state=receipt["issue_state"],
                issue_number=receipt.get("issue_number"),
                issue_url=receipt.get("issue_url"),
                duplicate=receipt["duplicate"],
                attempts=attempt_number,
                error_code=receipt.get("status_error"),
            )
        if not (retry_ambiguous and attempt.ambiguous and attempt_number < max_attempts):
            return FeedbackDeliveryResult(
                status="pending",
                attempts=attempt_number,
                error_code=attempt.error_code or "receiver_unavailable",
            )
    return FeedbackDeliveryResult(
        status="pending", attempts=max_attempts, error_code="receiver_unavailable",
    )
