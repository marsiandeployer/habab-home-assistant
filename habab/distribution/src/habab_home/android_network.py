"""Read validated Android network metadata shared by Core status and voice."""

from __future__ import annotations

import ipaddress
import json
import math
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

from .runtime_context import ANDROID_PROOT_RUNTIME_KIND, RUNTIME_KIND_ENV

NETWORK_SNAPSHOT_PATH = Path("/run/habab-network.json")
MAX_SNAPSHOT_BYTES = 65536
NETWORK_SNAPSHOT_MAX_AGE_SECONDS = 120
MAX_LAN_URLS = 32
PHONE_URL = "http://127.0.0.1:8123"
SOURCE = "android_connectivity_manager"


def load_network_snapshot(*, path=NETWORK_SNAPSHOT_PATH, now=None):
    """Load the existing atomic bridge snapshot, rejecting absent/stale input."""
    with open(Path(path), "rb") as snapshot_file:
        raw = snapshot_file.read(MAX_SNAPSHOT_BYTES + 1)
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise ValueError("network_snapshot_too_large")
    snapshot = json.loads(raw)
    if not isinstance(snapshot, dict) or type(snapshot.get("schema")) is not int or snapshot["schema"] != 1:
        raise ValueError("network_snapshot_schema_invalid")
    updated_at = snapshot.get("updatedAt")
    if (not isinstance(updated_at, (int, float)) or isinstance(updated_at, bool)
            or not math.isfinite(updated_at)):
        raise ValueError("network_snapshot_timestamp_invalid")
    current_time = time.time() if now is None else now
    if abs(current_time - updated_at) > NETWORK_SNAPSHOT_MAX_AGE_SECONDS:
        raise ValueError("network_snapshot_stale")
    if not isinstance(snapshot.get("interfaces"), list):
        raise ValueError("network_snapshot_interfaces_invalid")
    return snapshot


def _private_lan_url(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("network_snapshot_url_invalid")
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("network_snapshot_url_invalid")
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError as error:
        raise ValueError("network_snapshot_url_invalid") from error
    if (not isinstance(address, ipaddress.IPv4Address) or not address.is_private
            or address.is_loopback or address.is_link_local or address.is_unspecified
            or address.is_multicast or address.is_reserved or port != 8123):
        raise ValueError("network_snapshot_url_invalid")
    return f"http://{address}:8123"


def validated_web_access(snapshot):
    """Return only contract fields whose source and URL semantics are verified."""
    web_access = snapshot.get("web_access") if isinstance(snapshot, dict) else None
    if not isinstance(web_access, dict):
        raise ValueError("network_snapshot_web_access_missing")
    if (web_access.get("source") != SOURCE
            or web_access.get("external_reachability") != "not_verified"
            or web_access.get("phone_url") != PHONE_URL):
        raise ValueError("network_snapshot_web_access_invalid")

    candidates = web_access.get("lan_urls")
    if not isinstance(candidates, list) or len(candidates) > MAX_LAN_URLS:
        raise ValueError("network_snapshot_lan_urls_invalid")
    lan_urls = [_private_lan_url(value) for value in candidates]
    if len(set(lan_urls)) != len(lan_urls):
        raise ValueError("network_snapshot_lan_urls_invalid")
    selected = web_access.get("lan_url")
    lan_url = None if selected is None else _private_lan_url(selected)
    if lan_url is not None and lan_url not in lan_urls:
        raise ValueError("network_snapshot_lan_url_not_candidate")
    return {
        "status": "available",
        "phone_url": PHONE_URL,
        "phone_url_scope": "адрес Home Assistant на этом Android-телефоне",
        "lan_url": lan_url,
        "lan_urls": lan_urls,
        "lan_url_scope": "кандидат для клиента в той же локальной сети; доступность не проверена",
        "external_reachability": "not_verified",
        "source": SOURCE,
    }


def web_access_context(*, path=NETWORK_SNAPSHOT_PATH, now=None, runtime_kind=None):
    """Return validated URLs only in Android runtime; all other cases are unknown."""
    if runtime_kind is None:
        runtime_kind = os.environ.get(RUNTIME_KIND_ENV)
    if runtime_kind != ANDROID_PROOT_RUNTIME_KIND:
        return {"status": "unknown"}
    try:
        snapshot = load_network_snapshot(path=path, now=now)
        return validated_web_access(snapshot)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {"status": "unknown"}
