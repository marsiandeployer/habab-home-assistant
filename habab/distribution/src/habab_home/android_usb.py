"""Read the Android USB bridge status and expose its live serial endpoint to HA."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import stat
import time

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import callback
from homeassistant.setup import async_setup_component

from .runtime_context import ANDROID_PROOT_RUNTIME_KIND, RUNTIME_KIND_ENV

USB_STATUS_PATH = Path("/run/habab-ipc/usb-status.json")
USB_SOCKET_PATH = Path("/run/habab-ipc/cc2531.sock")
USB_DEVICE_PATH = f"unix://{USB_SOCKET_PATH}"
MAX_STATUS_BYTES = 4096
STATUS_MAX_AGE_SECONDS = 30
_STATUS_TOKEN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def _unavailable(reason: str) -> dict:
    return {"status": "unavailable", "reason": reason}


def read_usb_status(*, path=USB_STATUS_PATH, socket_path=USB_SOCKET_PATH,
                    now=None, runtime_kind=None) -> dict:
    """Return only a current, contract-checked status from the Android app."""
    if runtime_kind is None:
        runtime_kind = os.environ.get(RUNTIME_KIND_ENV)
    if runtime_kind != ANDROID_PROOT_RUNTIME_KIND:
        return _unavailable("not_android_runtime")

    try:
        with Path(path).open("rb") as status_file:
            raw = status_file.read(MAX_STATUS_BYTES + 1)
        if len(raw) > MAX_STATUS_BYTES:
            return _unavailable("status_too_large")
        data = json.loads(raw)
    except FileNotFoundError:
        return _unavailable("status_file_missing")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _unavailable("status_file_invalid")

    if not isinstance(data, dict):
        return _unavailable("status_contract_invalid")
    updated_at = data.get("updated_at")
    if (type(data.get("schema")) is not int or data["schema"] != 1
            or type(updated_at) not in (int, float)
            or isinstance(updated_at, float) and not math.isfinite(updated_at)
            or updated_at < 0 or updated_at > 253402300799):
        return _unavailable("status_contract_invalid")
    current_time = time.time() if now is None else now
    if abs(current_time - updated_at) > STATUS_MAX_AGE_SECONDS:
        return _unavailable("status_stale")

    expected = {
        "supported": True,
        "vid": 1105,
        "pid": 5800,
        "device_path": USB_DEVICE_PATH,
        "radio_type": "znp",
        "product": "CC2531",
    }
    if any(data.get(key) != value or type(data.get(key)) is not type(value)
           for key, value in expected.items()):
        return _unavailable("status_contract_invalid")
    for key in ("connected", "permission_granted", "bridge_ready"):
        if type(data.get(key)) is not bool:
            return _unavailable("status_contract_invalid")
    if data["bridge_ready"] and not (data["connected"] and data["permission_granted"]):
        return _unavailable("status_contract_invalid")
    status = data.get("status")
    message = data.get("message")
    if (not isinstance(status, str) or not _STATUS_TOKEN.fullmatch(status)
            or not isinstance(message, str) or len(message) > 256
            or any(ord(char) < 32 and char not in "\t" for char in message)):
        return _unavailable("status_contract_invalid")

    result = {
        "status": status,
        "updated_at": updated_at,
        "supported": True,
        "connected": data["connected"],
        "permission_granted": data["permission_granted"],
        "bridge_ready": data["bridge_ready"],
        "vid": data["vid"],
        "pid": data["pid"],
        "product": "CC2531",
        "radio_type": "znp",
        "message": message,
    }
    if data["bridge_ready"]:
        try:
            if not stat.S_ISSOCK(Path(socket_path).stat().st_mode):
                return {**result, "status": "unavailable",
                        "reason": "bridge_socket_missing", "bridge_ready": False}
        except OSError:
            return {**result, "status": "unavailable",
                    "reason": "bridge_socket_missing", "bridge_ready": False}
        result["device_path"] = USB_DEVICE_PATH
    return result


def scan_serial_ports(hass) -> list:
    """Advertise the bridge only while the private socket is ready and present."""
    status = read_usb_status()
    if status.get("bridge_ready") is not True or status.get("device_path") != USB_DEVICE_PATH:
        return []

    from homeassistant.components.usb import SerialDevice

    return [SerialDevice(
        device=USB_DEVICE_PATH,
        serial_number=None,
        manufacturer="Texas Instruments",
        description="CC2531 Android USB bridge",
        interface_description="Android USB CDC bridge",
    )]


async def async_setup(hass) -> bool:
    """Register a native serial-port scanner only in the Android PRoot runtime."""
    if os.environ.get(RUNTIME_KIND_ENV) != ANDROID_PROOT_RUNTIME_KIND:
        return True

    if "usb" not in hass.config.components:
        if not await async_setup_component(hass, "usb", {}):
            return True

    from homeassistant.components import usb

    remove_scanner = usb.async_register_serial_port_scanner(hass, scan_serial_ports)

    @callback
    def remove_on_stop(_event) -> None:
        remove_scanner()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, remove_on_stop)
    return True
