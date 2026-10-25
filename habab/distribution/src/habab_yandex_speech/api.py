"""Bounded Yandex SpeechKit API v1 requests."""

import asyncio
import json
from typing import Any
from urllib.parse import urlencode

import aiohttp

from homeassistant.helpers.aiohttp_client import async_get_clientsession


STT_URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
TTS_URL = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"
REQUEST_TIMEOUT = 25
MAX_AUDIO_BYTES = 1024 * 1024
MAX_AUDIO_DURATION_SECONDS = 30
MAX_TEXT_CHARS = 5000
MAX_TTS_REQUEST_BYTES = 15_000
MAX_AUDIO_RESPONSE_BYTES = 4 * 1024 * 1024


class SpeechKitError(Exception):
    """Safe error code that does not retain upstream response text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def validate_api_key(api_key: Any) -> str:
    """Accept only a bounded API-key form; credentials are never URL data."""
    if (
        not isinstance(api_key, str)
        or not 20 <= len(api_key) <= 512
        or not api_key.isascii()
        or any(ord(char) < 33 or ord(char) > 126 for char in api_key)
    ):
        raise SpeechKitError("invalid_auth")
    return api_key


class SpeechKitClient:
    """Call only the selected Yandex SpeechKit endpoints."""

    def __init__(self, hass, api_key: str) -> None:
        self.hass = hass
        self.api_key = validate_api_key(api_key)

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Api-Key {self.api_key}"}

    async def _request(
        self, method: str, url: str, *, params=None, data=None, headers=None,
    ) -> bytes:
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with async_get_clientsession(self.hass).request(
                    method,
                    url,
                    headers={**self._headers, **(headers or {})},
                    params=params,
                    data=data,
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                    allow_redirects=False,
                ) as response:
                    if response.status in (401, 403):
                        raise SpeechKitError("invalid_auth")
                    if response.status in (402, 429):
                        raise SpeechKitError("quota")
                    if response.status in (400, 413, 415):
                        raise SpeechKitError("invalid_request")
                    if response.status != 200:
                        raise SpeechKitError("cannot_connect")
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(16384):
                        body.extend(chunk)
                        if len(body) > MAX_AUDIO_RESPONSE_BYTES:
                            raise SpeechKitError("invalid_response")
                    if not body:
                        raise SpeechKitError("invalid_response")
                    return bytes(body)
        except SpeechKitError:
            raise
        except TimeoutError:
            raise SpeechKitError("timeout") from None
        except (aiohttp.ClientError, OSError):
            raise SpeechKitError("cannot_connect") from None

    async def recognize(self, audio: bytes, *, sample_rate: int, language: str) -> str:
        """Recognize raw mono LPCM with the synchronous STT API."""
        if (not isinstance(audio, bytes) or not audio or len(audio) > MAX_AUDIO_BYTES
                or len(audio) > sample_rate * 2 * MAX_AUDIO_DURATION_SECONDS):
            raise SpeechKitError("invalid_audio")
        if sample_rate not in (8000, 16000, 48000):
            raise SpeechKitError("invalid_audio")
        if language not in ("ru-RU",):
            raise SpeechKitError("unsupported_language")
        raw = await self._request(
            "POST",
            STT_URL,
            params={
                "format": "lpcm",
                "sampleRateHertz": str(sample_rate),
                "lang": language,
            },
            data=audio,
        )
        try:
            result = json.loads(raw).get("result")
        except (ValueError, UnicodeError, AttributeError):
            raise SpeechKitError("invalid_response") from None
        if not isinstance(result, str) or not result.strip():
            raise SpeechKitError("empty_transcript")
        return result.strip()

    async def synthesize(self, text: str, *, language: str = "ru-RU") -> bytes:
        """Synthesize Russian text and return the documented OggOpus stream."""
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
            raise SpeechKitError("invalid_text")
        if language not in ("ru-RU",):
            raise SpeechKitError("unsupported_language")
        body = urlencode({
            "text": text,
            "lang": language,
            "voice": "filipp",
        }).encode("ascii")
        if len(body) > MAX_TTS_REQUEST_BYTES:
            raise SpeechKitError("invalid_text")
        return await self._request(
            "POST",
            TTS_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
