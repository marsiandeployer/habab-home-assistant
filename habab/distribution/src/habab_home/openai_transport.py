"""Explicit HTTPS transport selection; no automatic retry on another route."""
import os
from urllib.parse import urlsplit


def base_url():
    value = os.environ.get("HABAB_OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or not parsed.path.endswith("/v1")):
        raise ValueError("invalid_openai_transport")
    return value


def endpoint(path):
    if path not in {"chat/completions", "audio/speech", "audio/transcriptions", "realtime/client_secrets"}:
        raise ValueError("invalid_openai_operation")
    return base_url() + "/" + path


def websocket_url(model):
    if not model.startswith("gpt-realtime") or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for c in model):
        raise ValueError("invalid_realtime_model")
    return "wss://" + base_url().split("://", 1)[1] + "/realtime?model=" + model
