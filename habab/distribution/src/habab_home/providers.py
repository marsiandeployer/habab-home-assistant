"""Bounded server-side transports for the three explicitly selected providers."""

import asyncio
import json
import re
from typing import Any

import aiohttp

PROVIDERS = {
    "openai": {
        "name": "OpenAI",
        "url": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4.1-mini",
    },
    "deepseek": {
        "name": "DeepSeek",
        "url": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-flash",
    },
    "gemini": {
        "name": "Google Gemini",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "model": "gemini-3.8-flash",
    },
}
MAX_RESPONSE_BYTES = 1024 * 1024
REQUEST_TIMEOUT = 25


class ProviderError(Exception):
    """A safe error code; never include upstream bodies or credentials."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def validate_settings(data: dict[str, Any]) -> dict[str, str]:
    """Validate technical identifiers, without interpreting natural language."""
    provider = data.get("provider")
    key = data.get("api_key")
    model = data.get("model")
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise ProviderError("invalid_provider")
    if not isinstance(key, str) or not 8 <= len(key.strip()) <= 512:
        raise ProviderError("invalid_auth")
    key = key.strip()
    if any(char.isspace() or ord(char) < 33 or ord(char) > 126 for char in key):
        raise ProviderError("invalid_auth")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", model):
        raise ProviderError("invalid_model")
    return {"provider": provider, "api_key": key, "model": model}


class ProviderClient:
    """Use documented OpenAI-compatible APIs, with no cross-provider fallback."""

    def __init__(self, session: aiohttp.ClientSession, settings: dict[str, Any]) -> None:
        self.session = session
        self.settings = validate_settings(settings)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        probe: bool = False,
    ) -> dict[str, Any]:
        provider = self.settings["provider"]
        body: dict[str, Any] = {
            "model": self.settings["model"],
            "messages": messages,
            "stream": False,
            "max_completion_tokens": 256 if probe else 1024,
        }
        if provider == "deepseek":
            # DeepSeek documents max_tokens and explicitly disabling thinking
            # for the plain Chat Completion tool-calling loop.
            body["max_tokens"] = body.pop("max_completion_tokens")
            body["thinking"] = {"type": "disabled"}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = (
                {"type": "function", "function": {"name": "habab_connection_test"}}
                if probe else "auto"
            )
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with self.session.post(
                    PROVIDERS[provider]["url"],
                    headers={"Authorization": f"Bearer {self.settings['api_key']}"},
                    json=body,
                    allow_redirects=False,
                ) as response:
                    if response.status in (401, 403):
                        raise ProviderError("invalid_auth")
                    if response.status in (402, 429):
                        raise ProviderError("quota")
                    if response.status in (400, 404):
                        raise ProviderError("invalid_model")
                    if response.status != 200:
                        raise ProviderError("cannot_connect")
                    chunks = bytearray()
                    async for chunk in response.content.iter_chunked(16384):
                        chunks.extend(chunk)
                        if len(chunks) > MAX_RESPONSE_BYTES:
                            raise ProviderError("invalid_response")
                    payload = json.loads(chunks)
        except TimeoutError:
            raise ProviderError("timeout") from None
        except (aiohttp.ClientError, OSError):
            raise ProviderError("cannot_connect") from None
        except (ValueError, UnicodeError):
            raise ProviderError("invalid_response") from None
        try:
            choice = payload["choices"][0]
            message = choice["message"]
            if choice.get("finish_reason") not in ("stop", "tool_calls"):
                raise ProviderError("invalid_response")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise ProviderError("invalid_response")
            if message.get("content") is not None and not isinstance(message["content"], str):
                raise ProviderError("invalid_response")
            return message
        except (KeyError, IndexError, TypeError):
            raise ProviderError("invalid_response") from None

    async def check(self) -> None:
        """Test the selected model and tool calling, without touching devices."""
        tool = {
            "type": "function",
            "function": {
                "name": "habab_connection_test",
                "description": "Check the connection. This tool has no side effects.",
                "parameters": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
            },
        }
        message = await self.complete(
            [{"role": "user", "content": "Call habab_connection_test with ok=true."}],
            [tool], probe=True,
        )
        try:
            calls = message["tool_calls"]
            if len(calls) != 1 or calls[0]["function"]["name"] != "habab_connection_test":
                raise ProviderError("tools_unsupported")
            arguments = json.loads(calls[0]["function"]["arguments"])
            if not isinstance(arguments, dict) or set(arguments) != {"ok"} or arguments["ok"] is not True:
                raise ProviderError("tools_unsupported")
        except (KeyError, TypeError, ValueError):
            raise ProviderError("tools_unsupported") from None
