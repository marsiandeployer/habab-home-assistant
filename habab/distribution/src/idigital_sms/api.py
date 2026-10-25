"""Small, single-recipient I-Digital Direct API client."""

import asyncio
import base64
import json
from urllib.parse import quote
from uuid import UUID

try:
    from aiohttp import ClientError
except ImportError:  # The Home Assistant runtime provides aiohttp.
    class ClientError(Exception):
        """Fallback type for standalone protocol tests."""

from .const import API_BASE, MAX_RESPONSE_BYTES, REQUEST_TIMEOUT


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


def _message_status(provider_status: object) -> str:
    """Map only provider states whose meaning is known; preserve no raw response."""
    if not isinstance(provider_status, str):
        return "unknown"
    status = provider_status.lower()
    if status == "delivered":
        return "delivered"
    if status in {"undelivered", "expired", "unsent", "stopped"}:
        return "error"
    if status in {"queued", "sending", "sent"}:
        return "accepted"
    return "unknown"


class DirectClient:
    """Send and explicitly query status for one configured recipient."""

    def __init__(self, session, *, api_key: str, sender: str, recipient: str) -> None:
        self._session = session
        self._api_key = validate_api_key(api_key)
        self._sender = validate_sender(sender)
        self._recipient = validate_recipient(recipient)

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Basic {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _request(self, method: str, path: str, *, payload=None) -> tuple[int, object]:
        url = f"{API_BASE}/{path.lstrip('/')}"

        async def request_once() -> tuple[int, object]:
            async with self._session.request(
                method,
                url,
                headers=self._headers,
                json=payload,
                allow_redirects=False,
            ) as response:
                raw = await response.content.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise IDigitalError("invalid_response")
                try:
                    body = json.loads(raw)
                except (ValueError, UnicodeError):
                    body = None
                return response.status, body

        try:
            return await asyncio.wait_for(request_once(), timeout=REQUEST_TIMEOUT)
        except IDigitalError:
            raise
        except asyncio.TimeoutError:
            raise IDigitalError("unknown_outcome") from None
        except (ClientError, OSError):
            raise IDigitalError("unknown_outcome") from None

    async def send_message(self, message: str) -> str:
        """Send exactly one SMS; network ambiguity is returned without retry."""
        content = validate_message(message)
        payload = [{
            "channelType": "SMS",
            "senderName": self._sender,
            "destination": self._recipient[1:],
            "content": content,
        }]
        http_status, body = await self._request("POST", "/message", payload=payload)
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
        if (body.get("errors") is not False or not isinstance(items, list)
                or len(items) != 1 or not isinstance(items[0], dict)):
            raise IDigitalError("unknown_outcome")
        message_uuid = items[0].get("messageUuid")
        if not isinstance(message_uuid, str):
            raise IDigitalError("unknown_outcome")
        try:
            return str(UUID(message_uuid))
        except ValueError:
            raise IDigitalError("unknown_outcome") from None

    async def get_status(self, message_uuid: str) -> dict[str, object]:
        """Make one explicit GET; never poll or retry in the background."""
        try:
            normalized_uuid = str(UUID(message_uuid))
        except (ValueError, TypeError, AttributeError):
            raise IDigitalError("invalid_message_uuid") from None
        http_status, body = await self._request(
            "GET", f"/message/{quote(normalized_uuid, safe='')}"
        )
        if http_status == 404:
            return {"status": "unknown", "provider_status": "not_found"}
        if http_status in (401, 403):
            raise IDigitalError("authentication_error")
        if 400 <= http_status < 500:
            raise IDigitalError("status_unavailable")
        if http_status >= 500 or http_status != 200 or not isinstance(body, dict):
            raise IDigitalError("status_unavailable")
        provider_status = body.get("status")
        return {
            "status": _message_status(provider_status),
            "provider_status": (
                provider_status.lower() if isinstance(provider_status, str) else "unknown"
            ),
        }
