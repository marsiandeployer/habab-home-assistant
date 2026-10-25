"""Save explicitly consented feedback from the current owner web-chat turn."""

from __future__ import annotations

import uuid

from .const import DOMAIN
from .diagnostics import (
    MAX_MESSAGES,
    ProjectionBudget,
    _mapped_sessions,
    _redact_text,
    _session_data,
)
from .feedback import (
    MAX_CONTEXT_BYTES,
    MAX_CONTEXT_EVENTS,
    MAX_FEEDBACK_CHARS,
    IDEMPOTENCY_KEY,
    FeedbackError,
    FeedbackManager,
    _digest,
    _public_receipt,
)


async def save_feedback_action(hass, setup_agent, owner: str, body: dict) -> dict:
    """Save a feedback receipt for the server-mapped web session in this turn.

    The model supplies feedback, explicit consent, and an idempotency UUID. It
    cannot select an owner, channel, or session ID.
    """
    if (
        not isinstance(body, dict)
        or set(body) != {"action", "feedback", "consent", "idempotency_key"}
    ):
        raise FeedbackError("invalid_feedback")
    feedback = body.get("feedback")
    key = body.get("idempotency_key")
    if (
        not isinstance(feedback, str)
        or not 1 <= len(feedback.strip()) <= MAX_FEEDBACK_CHARS
        or body.get("consent") is not True
        or not isinstance(key, str)
        or not IDEMPOTENCY_KEY.fullmatch(key)
    ):
        raise FeedbackError("invalid_feedback")
    try:
        key = str(uuid.UUID(key))
    except (ValueError, TypeError, AttributeError) as error:
        raise FeedbackError("invalid_feedback") from error

    active = getattr(setup_agent, "active", None)
    if (
        not isinstance(active, tuple)
        or len(active) != 2
        or active[0] != owner
        or not isinstance(active[1], str)
    ):
        raise FeedbackError("feedback_session_unavailable")
    session_id = active[1]

    domain_data = hass.data.get(DOMAIN, {})
    assistant = domain_data.get("assistant")
    manager = domain_data.get("feedback")
    if assistant is None or not isinstance(manager, FeedbackManager):
        raise FeedbackError("feedback_unavailable")

    # SetupAgent.active is the trusted local-call context. Require its session
    # to remain the owner's current web mapping; Telegram sessions are rejected.
    if getattr(assistant, "web_sessions", {}).get(owner) != session_id:
        raise FeedbackError("feedback_web_session_required")
    mappings = [
        mapping
        for mapping in _mapped_sessions(assistant, owner)
        if mapping["channel"] == "web" and mapping["session_id"] == session_id
    ]
    if len(mappings) != 1:
        raise FeedbackError("feedback_session_unavailable")
    mapping = mappings[0]

    normalized_feedback = feedback.strip()
    digest = _digest(session_id, normalized_feedback)
    existing = await manager.idempotent_record(owner, key, digest)
    if existing is not None:
        return _public_receipt(existing, duplicate=True)

    budget = ProjectionBudget(max_bytes=MAX_CONTEXT_BYTES, max_events=MAX_CONTEXT_EVENTS)
    session, events, _messages = await _session_data(
        hass, owner, mapping, budget=budget)
    if (
        getattr(setup_agent, "active", None) != active
        or getattr(assistant, "web_sessions", {}).get(owner) != session_id
    ):
        raise FeedbackError("feedback_session_unavailable")

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
        owner_id=owner,
        key=key,
        digest=digest,
        feedback=safe_feedback,
        context=context,
    )
    return _public_receipt(record)
