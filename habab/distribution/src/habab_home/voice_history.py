"""Strict codec for individually observed native voice history events."""

import hashlib
import json
import re


VOICE_EVENT_PROTOCOL = "native_gemini_event.v1"
VOICE_EVENT_PROTOCOL_KEY = "habab_voice_event_protocol"
VOICE_EVENT_METADATA_KEY = VOICE_EVENT_PROTOCOL_KEY
VOICE_EVENT_ID_KEY = "habab_voice_event_id"
VOICE_EVENT_HASH_KEY = "habab_voice_event_sha256"
VOICE_EVENT_PROVIDER_KEY = "habab_voice_provider"
VOICE_EVENT_ID_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


def valid_voice_event_fields(event_id, role, text, provider="gemini", correlation=None):
    """Validate the exact event contract accepted from the native client."""
    if (not isinstance(event_id, str) or not VOICE_EVENT_ID_PATTERN.fullmatch(event_id)
            or not isinstance(role, str) or role not in {"user", "assistant"}
            or not isinstance(text, str) or not text.strip()
            or len(text) > (4000 if role == "user" else 12000)
            or provider != "gemini" or correlation is not None):
        return False
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def encode_voice_event(event_id, role, text, provider="gemini", correlation=None):
    """Return canonical synthetic-part text, digest, and integrity metadata."""
    payload = {
        "protocol": VOICE_EVENT_PROTOCOL,
        "event_id": event_id,
        "role": role,
        "text": text,
        "provider": provider,
        "correlation": correlation,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    metadata = {
        VOICE_EVENT_PROTOCOL_KEY: VOICE_EVENT_PROTOCOL,
        VOICE_EVENT_ID_KEY: event_id,
        VOICE_EVENT_HASH_KEY: digest,
        VOICE_EVENT_PROVIDER_KEY: provider,
    }
    return raw, digest, metadata


def decode_voice_event_part(part):
    """Validate and project one synthetic event; malformed markers stay hidden."""
    if (not isinstance(part, dict) or part.get("type") != "text"
            or part.get("synthetic") is not True):
        return None
    metadata = part.get("metadata")
    if (not isinstance(metadata, dict)
            or set(metadata) != {VOICE_EVENT_PROTOCOL_KEY, VOICE_EVENT_ID_KEY,
                                 VOICE_EVENT_HASH_KEY, VOICE_EVENT_PROVIDER_KEY}
            or metadata.get(VOICE_EVENT_PROTOCOL_KEY) != VOICE_EVENT_PROTOCOL):
        return None
    event_id = metadata.get(VOICE_EVENT_ID_KEY)
    digest = metadata.get(VOICE_EVENT_HASH_KEY)
    provider = metadata.get(VOICE_EVENT_PROVIDER_KEY)
    raw = part.get("text")
    if (not isinstance(event_id, str) or not VOICE_EVENT_ID_PATTERN.fullmatch(event_id)
            or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or provider != "gemini"
            or not isinstance(raw, str)):
        return None
    try:
        raw_digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        return None
    if raw_digest != digest:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if (not isinstance(payload, dict)
            or set(payload) != {"protocol", "event_id", "role", "text", "provider", "correlation"}
            or payload.get("protocol") != VOICE_EVENT_PROTOCOL
            or payload.get("event_id") != event_id
            or payload.get("provider") != provider
            or not valid_voice_event_fields(
                event_id, payload.get("role"), payload.get("text"), provider,
                payload.get("correlation"))):
        return None
    return {"role": payload["role"], "text": payload["text"]}
