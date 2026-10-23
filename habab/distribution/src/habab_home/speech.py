"""Bounded OpenAI speech transport shared by native Assist STT and TTS."""

import asyncio

import aiohttp

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .providers import ProviderError, provider_api_base

ENDPOINTS = {
    "transcribe": "https://api.openai.com/v1/audio/transcriptions",
    "speak": "https://api.openai.com/v1/audio/speech",
}


async def request_audio(hass, entry, operation, *, data=None, json=None):
    if entry.data.get("provider") not in {"habab", "openai"}:
        raise ProviderError("invalid_provider")
    endpoint = provider_api_base(entry.data["provider"]) + ("/audio/transcriptions" if operation == "transcribe" else "/audio/speech")
    try:
        async with asyncio.timeout(40):
            async with async_get_clientsession(hass).post(
                endpoint,
                headers={"Authorization": f"Bearer {entry.data['api_key']}"},
                data=data, json=json, allow_redirects=False,
            ) as response:
                if response.status in (401, 403):
                    raise ProviderError("invalid_auth")
                if response.status in (402, 429):
                    raise ProviderError("quota")
                if response.status != 200:
                    raise ProviderError("speech_failed")
                result = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    result.extend(chunk)
                    if len(result) > 4 * 1024 * 1024:
                        raise ProviderError("invalid_response")
                if not result:
                    raise ProviderError("invalid_response")
                return bytes(result)
    except TimeoutError:
        raise ProviderError("timeout") from None
    except (aiohttp.ClientError, OSError):
        raise ProviderError("cannot_connect") from None
