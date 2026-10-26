"""Transport-independent I-Digital Direct message protocol helpers.

These small parsing and validation primitives are isolated from Home Assistant,
CRM storage and HTTP libraries so an adapter can supply its own transport.
"""

import base64
from uuid import UUID


class IDigitalError(Exception):
    """Safe provider error code; never retains provider text or credentials."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def validate_api_key(value: object) -> str:
    """Accept the Direct API's existing Base64 Basic token without re-encoding."""
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise IDigitalError("invalid_api_key")
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except (ValueError, UnicodeError):
        raise IDigitalError("invalid_api_key") from None
    node_id, separator, password = decoded.partition(":")
    if not separator or not node_id.isdigit() or not password:
        raise IDigitalError("invalid_api_key")
    if base64.b64encode(decoded.encode("utf-8")).decode("ascii") != value:
        raise IDigitalError("invalid_api_key")
    return value


def validate_recipient(value: object) -> str:
    """Check E.164 syntax locally; it does not determine whether the number is mobile."""
    if not isinstance(value, str) or not value.startswith("+"):
        raise IDigitalError("invalid_recipient")
    digits = value[1:]
    if not 7 <= len(digits) <= 15 or not digits.isascii() or not digits.isdigit():
        raise IDigitalError("invalid_recipient")
    if digits.startswith("0"):
        raise IDigitalError("invalid_recipient")
    return value


def validate_sender(value: object) -> str:
    """Reject empty or control-character sender names; provider approval is checked on send."""
    if not isinstance(value, str) or not value.strip():
        raise IDigitalError("invalid_sender")
    sender = value.strip()
    if any(not character.isprintable() for character in sender):
        raise IDigitalError("invalid_sender")
    return sender


_GSM7_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ ÆæßÉ"
    " !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM7_EXT = set("\f^{}\\[~]|€")


def validate_message(value: object) -> str:
    """Require one SMS part, using Direct's published 160/70-character boundaries."""
    if not isinstance(value, str) or not value.strip():
        raise IDigitalError("invalid_message")
    if all(character in _GSM7_BASIC or character in _GSM7_EXT for character in value):
        encoded_length = sum(2 if character in _GSM7_EXT else 1 for character in value)
        if encoded_length > 160:
            raise IDigitalError("message_too_long")
    else:
        # UTF-16 code units conservatively count supplementary Unicode characters.
        encoded_length = len(value.encode("utf-16-be")) // 2
        if encoded_length > 70:
            raise IDigitalError("message_too_long")
    return value


def build_sms_payload(sender: str, recipient: str, content: str) -> list[dict[str, str]]:
    """Build the one-message Direct request body used by both single-send adapters."""
    return [{
        "channelType": "SMS",
        "senderName": sender,
        "destination": recipient[1:],
        "content": content,
    }]


def parse_send_response(http_status: int, body: object) -> str:
    """Return the accepted message UUID or a safe provider outcome code."""
    if http_status in (401, 403):
        raise IDigitalError("authentication_error")
    if 400 <= http_status < 500:
        raise IDigitalError("provider_rejected")
    if http_status >= 500:
        raise IDigitalError("unknown_outcome")
    if http_status != 200 or not isinstance(body, dict):
        raise IDigitalError("unknown_outcome")
    if body.get("errors") is True:
        raise IDigitalError("provider_rejected")
    items = body.get("items")
    if (
        body.get("errors") is not False
        or not isinstance(items, list)
        or len(items) != 1
        or not isinstance(items[0], dict)
    ):
        raise IDigitalError("unknown_outcome")
    message_uuid = items[0].get("messageUuid")
    if not isinstance(message_uuid, str):
        raise IDigitalError("unknown_outcome")
    try:
        return str(UUID(message_uuid))
    except ValueError:
        raise IDigitalError("unknown_outcome") from None


def message_status(provider_status: object) -> str:
    """Map documented Direct states while leaving unknown values unclassified."""
    if not isinstance(provider_status, str):
        return "unknown"
    status = provider_status.lower()
    if status == "delivered":
        return "delivered"
    if status == "undelivered":
        return "error"
    if status == "sent":
        return "accepted"
    return "unknown"


def parse_status_response(http_status: int, body: object) -> dict[str, object]:
    """Normalize one UUID lookup without implying that a missing record was delivered."""
    if http_status == 404:
        return {"status": "unknown", "provider_status": "not_found"}
    if http_status in (401, 403):
        raise IDigitalError("authentication_error")
    if 400 <= http_status < 500:
        raise IDigitalError("status_unavailable")
    if http_status >= 500 or http_status != 200 or not isinstance(body, dict):
        raise IDigitalError("status_unavailable")
    provider_status = body.get("status")
    normalized_provider_status = (
        provider_status.lower()
        if isinstance(provider_status, str)
        and provider_status.lower() in {"sent", "delivered", "undelivered"}
        else "unknown"
    )
    return {
        "status": message_status(provider_status),
        "provider_status": normalized_provider_status,
    }
