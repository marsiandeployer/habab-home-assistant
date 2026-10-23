"""Explicit, optional HTTPS Bot API relay; the default remains Telegram direct."""
import os
from urllib.parse import urlsplit


def relay_base():
    value = os.environ.get("HABAB_TELEGRAM_RELAY", "").rstrip("/")
    if not value:
        return None
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError("invalid_telegram_relay")
    return value


def get_me_request(token):
    relay = relay_base()
    if relay:
        return relay + "/telegram/getMe", {"Authorization": "Bearer " + token}
    return "https://api.telegram.org/bot" + token + "/getMe", {}
