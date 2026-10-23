"""Explicit, optional HTTPS Bot API relay; the default remains Telegram direct."""
import os
from urllib.parse import urlsplit


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


def relay_base():
    value = os.environ.get("HABAB_TELEGRAM_RELAY", "").rstrip("/")
    if not value:
        return None
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError("invalid_telegram_relay")
    return value


def get_me_request(token, proxy=None):
    relay = None if proxy_url(proxy) else relay_base()
    if relay:
        return relay + "/telegram/getMe", {"Authorization": "Bearer " + token}
    return "https://api.telegram.org/bot" + token + "/getMe", {}
