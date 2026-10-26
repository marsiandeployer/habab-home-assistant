"""Small, single-recipient I-Digital Direct API client."""

import asyncio
import hashlib
import json
import time
from urllib.parse import quote
from uuid import UUID

try:
    from aiohttp import ClientError
except ImportError:  # The Home Assistant runtime provides aiohttp.
    class ClientError(Exception):
        """Fallback type for standalone protocol tests."""

from .const import API_BASE, MAX_RESPONSE_BYTES, REQUEST_TIMEOUT
from .direct_protocol import (
    IDigitalError,
    build_sms_payload,
    message_status as _message_status,
    parse_send_response,
    parse_status_response,
    validate_api_key,
    validate_message,
    validate_recipient,
    validate_sender,
)


class SendGuard:
    """Small in-memory per-entry rate limit and duplicate-attempt guard."""

    def __init__(self, *, clock=time.monotonic) -> None:
        self._clock = clock
        self._attempts: list[float] = []
        self._recent_digests: dict[str, float] = {}

    def reserve(self, message: str) -> None:
        """Reserve before POST so ambiguous attempts cannot be retried immediately."""
        now = self._clock()
        hour_ago = now - 3600
        self._attempts = [stamp for stamp in self._attempts if stamp > hour_ago]
        duplicate_cutoff = now - 300
        self._recent_digests = {
            digest: stamp for digest, stamp in self._recent_digests.items()
            if stamp > duplicate_cutoff
        }
        digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
        if digest in self._recent_digests:
            raise IDigitalError("duplicate_suppressed")
        if self._attempts and now - self._attempts[-1] < 60:
            raise IDigitalError("rate_limited")
        if len(self._attempts) >= 10:
            raise IDigitalError("rate_limited")
        self._attempts.append(now)
        self._recent_digests[digest] = now


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
                raw = bytearray()
                async for chunk in response.content.iter_chunked(MAX_RESPONSE_BYTES + 1):
                    raw.extend(chunk)
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
        payload = build_sms_payload(self._sender, self._recipient, content)
        http_status, body = await self._request("POST", "/message", payload=payload)
        return parse_send_response(http_status, body)

    async def get_status(self, message_uuid: str) -> dict[str, object]:
        """Make one explicit GET; never poll or retry in the background."""
        try:
            normalized_uuid = str(UUID(message_uuid))
        except (ValueError, TypeError, AttributeError):
            raise IDigitalError("invalid_message_uuid") from None
        http_status, body = await self._request(
            "GET", f"/message/{quote(normalized_uuid, safe='')}"
        )
        return parse_status_response(http_status, body)
