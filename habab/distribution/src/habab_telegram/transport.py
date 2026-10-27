"""Resolve Telegram Bot API transport from the selected HA provider."""

from collections.abc import Mapping
import json
import os
from urllib.parse import urlsplit

import aiohttp


TELEGRAM_PROTOCOL = "telegram-bot-api-relay-v1"
TELEGRAM_BASE_PATH = "/telegram"
TELEGRAM_METHODS = frozenset({
    "getMe", "getUpdates", "getChatMember", "sendMessage", "sendChatAction",
})
CAPABILITIES_PATH = "/v1/capabilities"
MAX_CAPABILITIES_BYTES = 256 * 1024
CAPABILITIES_TIMEOUT = 10


class TransportError(ValueError):
    """Safe transport error code; never includes a token, URL, or response body."""


def proxy_url(value):
    """Validate a per-bot HTTP(S) proxy without including credentials in errors."""
    if value is None or value == "":
        return None
    if (not isinstance(value, str) or len(value) > 2048
            or any(char.isspace() or ord(char) < 32 for char in value)):
        raise ValueError("invalid_proxy")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.query or parsed.fragment or parsed.path not in ("", "/")
                or port == 0):
            raise ValueError
    except ValueError:
        raise ValueError("invalid_proxy") from None
    return value


def worker_proxy_env(value):
    """Scope Node's built-in proxy support to this bot's worker process."""
    proxy = proxy_url(value)
    if not proxy:
        return {}
    return {"NODE_USE_ENV_PROXY": "1", "HTTP_PROXY": proxy,
            "HTTPS_PROXY": proxy, "NO_PROXY": ""}


def relay_origin():
    """Derive the fixed relay origin from the built-in Habab provider API base."""
    from homeassistant.components.habab_home.providers import PROVIDERS

    parsed = urlsplit(PROVIDERS["habab"]["url"])
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise TransportError("relay_origin_invalid")
    return f"{parsed.scheme}://{parsed.netloc}"


async def _read_limited(response, limit=MAX_CAPABILITIES_BYTES):
    payload = bytearray()
    async for chunk in response.content.iter_chunked(8192):
        payload.extend(chunk)
        if len(payload) > limit:
            raise TransportError("relay_capabilities_invalid")
    return bytes(payload)


async def validate_relay_capabilities(session, service_key):
    """Require the narrow Telegram relay contract before using a service key."""
    if not isinstance(service_key, str) or not service_key:
        raise TransportError("relay_auth_unavailable")
    url = relay_origin() + CAPABILITIES_PATH
    try:
        async with session.get(
            url, headers={"Authorization": f"Bearer {service_key}"},
            timeout=aiohttp.ClientTimeout(total=CAPABILITIES_TIMEOUT),
            allow_redirects=False,
        ) as response:
            if response.status in (401, 403):
                raise TransportError("relay_auth_failed")
            if response.status != 200 or response.headers.get("Location"):
                raise TransportError("relay_capabilities_unavailable")
            try:
                payload = json.loads(await _read_limited(response))
            except (ValueError, UnicodeError):
                raise TransportError("relay_capabilities_invalid") from None
    except TransportError:
        raise
    except Exception:
        raise TransportError("relay_capabilities_unavailable") from None

    contract = payload.get("telegram") if isinstance(payload, dict) else None
    methods = contract.get("methods") if isinstance(contract, dict) else None
    if (not isinstance(contract, dict)
            or contract.get("protocol") != TELEGRAM_PROTOCOL
            or contract.get("base") != TELEGRAM_BASE_PATH
            or not isinstance(methods, list)
            or any(not isinstance(method, str) for method in methods)
            or not TELEGRAM_METHODS.issubset(methods)):
        raise TransportError("relay_capabilities_incompatible")
    return {"protocol": TELEGRAM_PROTOCOL, "base": TELEGRAM_BASE_PATH}


def _selected_settings(hass):
    data = getattr(hass, "data", {}).get("habab_home", {})
    setup_agent = data.get("setup_agent") if isinstance(data, dict) else None
    settings_method = getattr(setup_agent, "settings", None)
    if not callable(settings_method):
        return None
    try:
        settings = settings_method()
    except Exception as error:
        if getattr(error, "code", None) == "provider_required":
            return None
        raise TransportError("selected_provider_unavailable") from None
    if not isinstance(settings, Mapping):
        raise TransportError("selected_provider_unavailable")
    return settings


def _transport_identity_and_settings(hass, proxy=None):
    proxy = proxy_url(proxy)
    if proxy:
        return ("direct_proxy",), None, None
    settings = _selected_settings(hass)
    if settings is None:
        if os.environ.get("HABAB_TELEGRAM_RELAY"):
            raise TransportError("unsupported_legacy_relay")
        return ("direct",), None, None
    provider = settings.get("provider")
    if not isinstance(provider, str) or provider not in {"habab", "openai", "deepseek", "gemini"}:
        raise TransportError("selected_provider_invalid")
    if provider != "habab":
        if os.environ.get("HABAB_TELEGRAM_RELAY"):
            raise TransportError("unsupported_legacy_relay")
        return ("direct",), settings, provider
    service_key = settings.get("api_key")
    if not isinstance(service_key, str) or not service_key:
        raise TransportError("relay_auth_unavailable")
    return ("habab_relay", relay_origin(), service_key), settings, provider


def telegram_transport_identity(hass, proxy=None):
    """In-memory effective route identity; never persist or log credential material."""
    identity, _settings, _provider = _transport_identity_and_settings(hass, proxy)
    return identity


async def resolve_telegram_transport(hass, session, proxy=None):
    """Return Node's stdin transport contract and selected provider ID."""
    proxy = proxy_url(proxy)
    identity, _settings, provider = _transport_identity_and_settings(hass, proxy)
    if identity[0] != "habab_relay":
        return {"mode": "direct"}, provider

    service_key = identity[2]
    await validate_relay_capabilities(session, service_key)
    return {
        "mode": "habab_relay",
        "base_url": identity[1],
        "service_key": service_key,
        "protocol": TELEGRAM_PROTOCOL,
    }, provider


def get_me_request(token, proxy=None, telegram_transport=None):
    """Build getMe request for the already resolved route; no environment routing."""
    if proxy_url(proxy):
        return "https://api.telegram.org/bot" + token + "/getMe", {}
    route = telegram_transport or {"mode": "direct"}
    if route.get("mode") == "direct":
        return "https://api.telegram.org/bot" + token + "/getMe", {}
    if route.get("mode") != "habab_relay":
        raise TransportError("telegram_transport_invalid")
    origin = relay_origin()
    if (route.get("base_url") != origin or route.get("protocol") != TELEGRAM_PROTOCOL
            or not isinstance(route.get("service_key"), str) or not route["service_key"]):
        raise TransportError("telegram_transport_invalid")
    return origin + "/telegram/getMe", {
        "Authorization": "Bearer " + route["service_key"],
        "X-Habab-Telegram-Bot-Token": token,
    }
