"""Explicit, category-based telemetry for Habab Home."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import traceback
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar
import probatio

from homeassistant.components import websocket_api
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, __version__ as HA_VERSION
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.system_info import async_get_system_info

from .const import DOMAIN, VERSION

RECEIVER_URL = "https://habab-demo.wpmix.net/api/ha-telemetry"
PREFERENCE_KEYS = ("base", "usage", "statistics", "diagnostics")
MAX_BODY_BYTES = 32 * 1024
MAX_RESPONSE_BYTES = 1024
MAX_DIAGNOSTICS_PER_DAY = 20
MAX_DIAGNOSTIC_QUEUE = 20
MIN_ANALYTICS_INTERVAL = timedelta(minutes=1)


def _default_preferences() -> dict[str, bool]:
    return dict.fromkeys(PREFERENCE_KEYS, False)


def _clean_diagnostic(record: logging.LogRecord) -> dict | None:
    """Build a diagnostic from exception metadata only; never inspect its message."""
    if not record.exc_info or not record.exc_info[0] or not record.exc_info[2]:
        return None

    error_type = record.exc_info[0].__name__
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", error_type):
        error_type = "Exception"

    logger_name = record.name
    for prefix in ("homeassistant.components.", "custom_components."):
        if logger_name.startswith(prefix):
            logger_name = logger_name[len(prefix):]
            break
    else:
        logger_name = "core"
    component = logger_name.split(".", 1)[0]
    if not re.fullmatch(r"[a-zA-Z0-9_.]{1,160}", component):
        component = "other"

    frames = []
    for frame in traceback.extract_tb(record.exc_info[2])[-12:]:
        filename = os.path.basename(frame.filename)
        function = frame.name
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", filename):
            filename = "unknown"
        if not re.fullmatch(r"[A-Za-z0-9_.<>]{1,100}", function):
            function = "unknown"
        frames.append({"file": filename, "function": function, "line": int(frame.lineno)})
    return {"error_type": error_type, "component": component, "frames": frames}


class _DiagnosticHandler(logging.Handler):
    def __init__(self, telemetry: TelemetryManager) -> None:
        super().__init__(level=logging.ERROR)
        self.telemetry = telemetry

    def emit(self, record: logging.LogRecord) -> None:
        if record.name in {"habab_home.telemetry", __name__}:
            return
        event = _clean_diagnostic(record)
        if event is not None:
            self.telemetry.hass.add_job(self.telemetry.async_enqueue_diagnostic, event)


class TelemetryManager:
    """Stores consent locally and sends only the selected, bounded categories."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.store = Store(hass, 1, "habab_home.telemetry", private=True)
        self.data = {
            "installation_id": uuid.uuid4().hex,
            "preferences": _default_preferences(),
            "last_delivery": None,
            "last_error": None,
            "last_analytics_attempt": None,
            "ever_opted_in": False,
            "diagnostic_day": None,
            "diagnostics_today": 0,
        }
        self._operation_lock = asyncio.Lock()
        self._diagnostic_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=MAX_DIAGNOSTIC_QUEUE)
        self._diagnostic_worker: asyncio.Task | None = None
        self._diagnostic_handler: _DiagnosticHandler | None = None
        self._startup_task: asyncio.Task | None = None
        self._unsubscribe_daily = None
        self._closed = False

    async def async_setup(self) -> None:
        stored = await self.store.async_load()
        if isinstance(stored, dict):
            self.data.update({key: stored[key] for key in self.data if key in stored})
        installation_id = self.data.get("installation_id")
        if not isinstance(installation_id, str) or not re.fullmatch(r"[a-f0-9]{32}", installation_id):
            self.data["installation_id"] = uuid.uuid4().hex
        self.data["preferences"] = self._normalize_preferences(self.data.get("preferences"))
        if any(self.data["preferences"].values()):
            self.data["ever_opted_in"] = True
        if not isinstance(self.data.get("diagnostics_today"), int):
            self.data["diagnostics_today"] = 0
        if not isinstance(self.data.get("ever_opted_in"), bool):
            self.data["ever_opted_in"] = any(self.data["preferences"].values())
        self.data["diagnostics_today"] = min(max(self.data["diagnostics_today"], 0), MAX_DIAGNOSTICS_PER_DAY)
        await self._save()
        self._configure_opt_ins()
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self.async_close)
        if self.data["preferences"]["base"]:
            self._startup_task = self.hass.async_create_task(self.async_send_analytics())

    @staticmethod
    def _normalize_preferences(value) -> dict[str, bool]:
        if not isinstance(value, dict):
            return _default_preferences()
        return {key: value.get(key) is True for key in PREFERENCE_KEYS}

    def _public_state(self) -> dict:
        return {
            "installation_id": self.data["installation_id"],
            "preferences": dict(self.data["preferences"]),
            "last_delivery": self.data["last_delivery"],
            "last_error": self.data["last_error"],
        }

    async def async_get_state(self) -> dict:
        return self._public_state()

    async def _save(self) -> None:
        await self.store.async_save(self.data)

    def _configure_opt_ins(self) -> None:
        preferences = self.data["preferences"]
        root_logger = logging.getLogger()
        if preferences["diagnostics"] and self._diagnostic_handler is None:
            self._diagnostic_handler = _DiagnosticHandler(self)
            root_logger.addHandler(self._diagnostic_handler)
        elif not preferences["diagnostics"] and self._diagnostic_handler is not None:
            root_logger.removeHandler(self._diagnostic_handler)
            self._diagnostic_handler.close()
            self._diagnostic_handler = None

        if preferences["base"] and self._unsubscribe_daily is None:
            self._unsubscribe_daily = async_track_time_interval(
                self.hass, self._daily_analytics, timedelta(days=1)
            )
        elif not preferences["base"] and self._unsubscribe_daily is not None:
            self._unsubscribe_daily()
            self._unsubscribe_daily = None

    async def async_set_preferences(self, preferences: dict) -> dict:
        if not isinstance(preferences, dict) or set(preferences) != set(PREFERENCE_KEYS):
            raise ValueError("invalid_preferences")
        if any(type(preferences[key]) is not bool for key in PREFERENCE_KEYS):
            raise ValueError("invalid_preferences")
        if not preferences["base"] and (preferences["usage"] or preferences["statistics"]):
            raise ValueError("base_required")

        async with self._operation_lock:
            previous_opt_in = self.data["ever_opted_in"]
            selected = dict(preferences)
            self.data["preferences"] = selected
            has_opt_in = any(selected.values())
            self.data["ever_opted_in"] = previous_opt_in or has_opt_in
            await self._save()
            self._configure_opt_ins()
            if not selected["diagnostics"]:
                self._clear_diagnostic_queue()

            if not has_opt_in:
                if previous_opt_in:
                    await self._post("withdraw", selected, {})
                return self._public_state()

            if not await self._post("preferences", selected, {}):
                return self._public_state()
            if selected["base"]:
                await self._send_analytics_locked()
            return self._public_state()

    async def _daily_analytics(self, _now) -> None:
        await self.async_send_analytics()

    async def async_send_analytics(self) -> None:
        if self._closed:
            return
        async with self._operation_lock:
            if not self._closed and self.data["preferences"]["base"]:
                await self._send_analytics_locked()

    async def _send_analytics_locked(self) -> None:
        preferences = self.data["preferences"]
        previous = self.data.get("last_analytics_attempt")
        if isinstance(previous, str):
            try:
                previous_time = datetime.fromisoformat(previous)
                if datetime.now(timezone.utc) - previous_time < MIN_ANALYTICS_INTERVAL:
                    await self._set_delivery_error("rate_limited")
                    return
            except (TypeError, ValueError):
                pass
        self.data["last_analytics_attempt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        await self._save()
        try:
            data = {"base": await self._base_data()}
            if preferences["usage"]:
                data["usage"] = self._usage_data()
            if preferences["statistics"]:
                data["statistics"] = await self._statistics_data()
        except Exception:
            await self._set_delivery_error("collection_error")
            return
        await self._post("analytics", preferences, data)

    async def _base_data(self) -> dict[str, str]:
        system = await async_get_system_info(self.hass)
        return {
            "ha_version": str(HA_VERSION),
            "habab_version": str(VERSION),
            "installation_type": str(system.get("installation_type") or "Unknown")[:80],
            "arch": str(system.get("arch") or "Unknown")[:80],
            "python_version": str(system.get("python_version") or "Unknown")[:40],
        }

    def _usage_data(self) -> dict:
        integrations = sorted(
            component for component in self.hass.config.components
            if isinstance(component, str) and re.fullmatch(r"[a-z0-9_]{1,80}", component)
        )[:500]
        registry = device_registry.async_get(self.hass)
        model_counts = Counter()
        for device in registry.devices:
            manufacturer = self._safe_model_value(device.manufacturer, device.name_by_user, device.name)
            model = self._safe_model_value(device.model, device.name_by_user, device.name)
            if manufacturer and model:
                model_counts[(manufacturer, model)] += 1
        device_models = [
            {"manufacturer": manufacturer, "model": model, "count": count}
            for (manufacturer, model), count in model_counts.most_common(100)
        ]
        return {"integrations": integrations, "device_models": device_models}

    @staticmethod
    def _safe_model_value(value, user_name, name) -> str:
        if not isinstance(value, str):
            return ""
        value = value.strip()[:80]
        if not value or value in {user_name, name}:
            return ""
        try:
            ipaddress.ip_address(value)
            return ""
        except ValueError:
            pass
        if re.search(r"(?i)(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}", value):
            return ""
        return value if re.fullmatch(r"[\w .+()-]{1,80}", value) else ""

    async def _statistics_data(self) -> dict[str, int]:
        devices = device_registry.async_get(self.hass).devices
        entities = entity_registry.async_get(self.hass).entities
        automations = self.hass.states.async_entity_ids("automation")
        users = await self.hass.auth.async_get_users()
        return {
            "devices": len(devices),
            "entities": len(entities),
            "automations": len(automations),
            "users": len(users),
        }

    async def async_enqueue_diagnostic(self, event: dict) -> None:
        if self._closed or not self.data["preferences"]["diagnostics"] or self._diagnostic_queue.full():
            return
        async with self._operation_lock:
            if self._closed or not self.data["preferences"]["diagnostics"] or self._diagnostic_queue.full():
                return
            today = datetime.now(timezone.utc).date().isoformat()
            if self.data["diagnostic_day"] != today:
                self.data["diagnostic_day"] = today
                self.data["diagnostics_today"] = 0
            if self.data["diagnostics_today"] >= MAX_DIAGNOSTICS_PER_DAY:
                return
            self.data["diagnostics_today"] += 1
            await self._save()
            self._diagnostic_queue.put_nowait(event)
            if self._diagnostic_worker is None or self._diagnostic_worker.done():
                self._diagnostic_worker = self.hass.async_create_task(self._drain_diagnostics())

    async def _drain_diagnostics(self) -> None:
        while not self._closed and not self._diagnostic_queue.empty():
            event = await self._diagnostic_queue.get()
            try:
                async with self._operation_lock:
                    preferences = self.data["preferences"]
                    if preferences["diagnostics"]:
                        await self._post("diagnostic", preferences, event)
            finally:
                self._diagnostic_queue.task_done()

    def _clear_diagnostic_queue(self) -> None:
        while not self._diagnostic_queue.empty():
            try:
                self._diagnostic_queue.get_nowait()
                self._diagnostic_queue.task_done()
            except asyncio.QueueEmpty:
                break

    async def _post(self, kind: str, preferences: dict, data: dict) -> bool:
        body = {
            "schema": 1,
            "installation_id": self.data["installation_id"],
            "kind": kind,
            "preferences": dict(preferences),
            "data": data,
        }
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_BODY_BYTES:
            await self._set_delivery_error("payload_too_large")
            return False
        try:
            async with ClientSession(
                cookie_jar=DummyCookieJar(), timeout=ClientTimeout(total=10)
            ) as session:
                async with session.post(
                    RECEIVER_URL,
                    data=encoded,
                    headers={"Content-Type": "application/json"},
                    allow_redirects=False,
                ) as response:
                    raw = await response.content.read(MAX_RESPONSE_BYTES + 1)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        await self._set_delivery_error("invalid_response")
                        return False
                    if response.status == 429:
                        await self._set_delivery_error("rate_limited")
                        return False
                    if response.status == 503:
                        await self._set_delivery_error("server_unavailable")
                        return False
                    if response.status != 202:
                        await self._set_delivery_error("server_rejected")
                        return False
                    try:
                        accepted = json.loads(raw).get("accepted") is True
                    except (ValueError, AttributeError):
                        accepted = False
                    if not accepted:
                        await self._set_delivery_error("invalid_response")
                        return False
        except (ClientError, TimeoutError, OSError):
            await self._set_delivery_error("network_error")
            return False

        self.data["last_delivery"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.data["last_error"] = None
        await self._save()
        return True

    async def _set_delivery_error(self, code: str) -> None:
        self.data["last_error"] = code
        await self._save()

    async def async_close(self, _event=None) -> None:
        self._closed = True
        if self._unsubscribe_daily is not None:
            self._unsubscribe_daily()
            self._unsubscribe_daily = None
        if self._diagnostic_handler is not None:
            logging.getLogger().removeHandler(self._diagnostic_handler)
            self._diagnostic_handler.close()
            self._diagnostic_handler = None
        self._clear_diagnostic_queue()
        if self._diagnostic_worker and not self._diagnostic_worker.done():
            self._diagnostic_worker.cancel()
            await asyncio.gather(self._diagnostic_worker, return_exceptions=True)
        if self._startup_task and not self._startup_task.done():
            self._startup_task.cancel()
            await asyncio.gather(self._startup_task, return_exceptions=True)


@callback
def async_register_commands(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, get_preferences)
    websocket_api.async_register_command(hass, set_preferences)


@websocket_api.websocket_command({probatio.Required("type"): "habab_home/telemetry/get"})
@websocket_api.require_admin
@websocket_api.async_response
async def get_preferences(hass, connection, msg) -> None:
    result = await hass.data[DOMAIN]["telemetry"].async_get_state()
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({
    probatio.Required("type"): "habab_home/telemetry/set",
    probatio.Required("preferences"): dict,
})
@websocket_api.require_admin
@websocket_api.async_response
async def set_preferences(hass, connection, msg) -> None:
    try:
        result = await hass.data[DOMAIN]["telemetry"].async_set_preferences(msg["preferences"])
    except ValueError as error:
        code = str(error)
        connection.send_error(msg["id"], code, code)
        return
    connection.send_result(msg["id"], result)
