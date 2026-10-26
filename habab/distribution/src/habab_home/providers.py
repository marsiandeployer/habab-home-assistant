"""Bounded server-side transports for explicitly selected providers."""

import asyncio
import importlib
import json
import re
from dataclasses import dataclass
from typing import Any

import aiohttp

PROVIDERS = {
    "habab": {
        "name": "Habab",
        "url": "https://habab-voice.wpmix.net/gemini/v1beta",
        "model": "gemini-3.1-flash-lite",
    },
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
        "url": "https://generativelanguage.googleapis.com/v1beta",
        "model": "gemini-3.1-flash-lite",
    },
}
MAX_RESPONSE_BYTES = 1024 * 1024
REQUEST_TIMEOUT = 25
HABAB_TEXT_ORIGIN = "https://habab-voice.wpmix.net"
HABAB_TEXT_CAPABILITIES_URL = HABAB_TEXT_ORIGIN + "/text/capabilities"
HABAB_TEXT_CAPABILITY_TIMEOUT = 8
HABAB_TEXT_CAPABILITY_MAX_BYTES = 16 * 1024


@dataclass(frozen=True)
class ProviderContract:
    """One resolved text transport shared by conversation and OpenCode.

    ``model`` is the request model ID for direct providers. For a managed
    provider it is the gateway alias, not the upstream provider's model ID.
    """

    provider: str
    protocol: str
    model: str
    base_url: str
    managed_model: str | None = None
    text_ready: bool | None = None
    configured_upstream_provider: str | None = None
    configured_upstream_model: str | None = None


def _direct_contract(settings: dict[str, str]) -> ProviderContract:
    provider = settings["provider"]
    protocol = "google" if provider in {"gemini", "habab"} else "openai_compatible"
    return ProviderContract(
        provider=provider,
        protocol=protocol,
        model=settings["model"],
        base_url=provider_api_base(provider),
    )


async def resolve_provider_contract(
    session: aiohttp.ClientSession | None, settings: dict[str, Any],
) -> ProviderContract:
    """Resolve a selected provider without accepting remote routing hints."""
    clean = validate_settings(settings)
    if clean["provider"] != "habab":
        return _direct_contract(clean)
    if session is None:
        raise ProviderError("cannot_connect")

    try:
        async with session.get(
            HABAB_TEXT_CAPABILITIES_URL,
            headers={"Authorization": f"Bearer {clean['api_key']}"},
            timeout=aiohttp.ClientTimeout(total=HABAB_TEXT_CAPABILITY_TIMEOUT),
            allow_redirects=False,
        ) as response:
            if response.status == 404:
                return _direct_contract(clean)
            if response.status == 401:
                raise ProviderError("invalid_auth")
            if response.status not in (200, 503):
                raise ProviderError("cannot_connect")
            if (response.content_length is not None
                    and response.content_length > HABAB_TEXT_CAPABILITY_MAX_BYTES):
                raise ProviderError("invalid_response")
            raw = await response.content.read(HABAB_TEXT_CAPABILITY_MAX_BYTES + 1)
            if len(raw) > HABAB_TEXT_CAPABILITY_MAX_BYTES:
                raise ProviderError("invalid_response")
    except ProviderError:
        raise
    except TimeoutError:
        raise ProviderError("timeout") from None
    except (aiohttp.ClientError, OSError):
        raise ProviderError("cannot_connect") from None

    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ProviderError("invalid_response") from None

    error = payload.get("error") if isinstance(payload, dict) else None
    if (response.status == 503 and isinstance(error, dict)
            and error.get("code") == "habab_text_upstream_not_ready"):
        return ProviderContract(
            provider="habab", protocol="openai_compatible", model="habab-chat",
            base_url=HABAB_TEXT_ORIGIN + "/v1", managed_model="habab-chat",
            text_ready=False,
        )
    if (not isinstance(payload, dict)
            or payload.get("protocol") != "openai_compatible"
            or payload.get("model") != "habab-chat"
            or payload.get("api_path") != "/v1"
            or payload.get("managed") is not True
            or not isinstance(payload.get("configured"), bool)
            or not isinstance(payload.get("ready"), bool)
            or payload["configured"] != payload["ready"]):
        raise ProviderError("invalid_response")
    upstream_provider = payload.get("configured_upstream_provider")
    upstream_model = payload.get("configured_upstream_model")
    upstream_is_valid = (
        isinstance(upstream_provider, str)
        and upstream_provider in {"deepseek", "openai", "gemini"}
        and isinstance(upstream_model, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", upstream_model)
    )
    return ProviderContract(
        provider="habab", protocol="openai_compatible", model="habab-chat",
        base_url=HABAB_TEXT_ORIGIN + "/v1", managed_model="habab-chat",
        text_ready=payload["ready"],
        configured_upstream_provider=upstream_provider if upstream_is_valid else None,
        configured_upstream_model=upstream_model if upstream_is_valid else None,
    )


def provider_api_base(provider: str) -> str:
    """Return the explicitly selected provider's API; never infer a relay."""
    if provider in {"gemini", "habab"}:
        return PROVIDERS[provider]["url"]
    return PROVIDERS[provider]["url"].removesuffix("/chat/completions")


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
    """Use the selected native or compatible API, with no provider fallback."""

    def __init__(self, session: aiohttp.ClientSession, settings: dict[str, Any]) -> None:
        self.session = session
        self.settings = validate_settings(settings)
        self.contract: ProviderContract | None = None

    async def resolved_contract(self) -> ProviderContract:
        if self.contract is None:
            self.contract = await resolve_provider_contract(self.session, self.settings)
        return self.contract

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        probe: bool = False,
    ) -> dict[str, Any]:
        provider = self.settings["provider"]
        contract = await self.resolved_contract()
        if contract.text_ready is False:
            raise ProviderError("provider_not_ready")
        if contract.protocol == "google":
            if not hasattr(self, "_gemini"):
                try:
                    async with asyncio.timeout(REQUEST_TIMEOUT):
                        module = await asyncio.to_thread(
                            importlib.import_module, f"{__package__}.providers_gemini",
                        )
                except TimeoutError:
                    raise ProviderError("timeout") from None
                except Exception:
                    raise ProviderError("cannot_connect") from None
                self._gemini = module.GeminiProvider(self.settings)
            return await self._gemini.complete(messages, tools, probe=probe)
        body: dict[str, Any] = {
            "model": contract.model,
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
                    contract.base_url + "/chat/completions",
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
        """Test the selected provider route and tool calling without device actions."""
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
