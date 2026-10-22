"""Opt-in telemetry contract and privacy boundaries."""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import DummyCookieJar
from homeassistant.components.habab_home import telemetry as telemetry_module
from homeassistant.components.habab_home.telemetry import TelemetryManager, _clean_diagnostic


class MemoryStore:
    def __init__(self):
        self.value = None

    async def async_load(self):
        return deepcopy(self.value)

    async def async_save(self, value):
        self.value = deepcopy(value)


class FakeResponse:
    status = 202

    def __init__(self, body=b'{"accepted":true}'):
        self.content = SimpleNamespace(read=self._read)
        self.body = body

    async def _read(self, _size):
        return self.body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class FakeSession:
    calls = []
    options = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.options.append(kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse()


class FakeBus:
    def async_listen_once(self, _event, callback):
        self.callback = callback


class FakeHass:
    def __init__(self):
        self.bus = FakeBus()
        self.config = SimpleNamespace(components=set())
        self.states = SimpleNamespace(async_entity_ids=lambda _domain: [])
        self.auth = SimpleNamespace(async_get_users=self._get_users)
        self.data = {}

    async def _get_users(self):
        return []

    def async_create_task(self, coro):
        return asyncio.create_task(coro)

    def add_job(self, target, *args):
        if asyncio.iscoroutinefunction(target):
            return self.async_create_task(target(*args))
        return target(*args)


class TelemetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeSession.calls = []
        FakeSession.options = []

    def make_manager(self):
        hass = FakeHass()
        store = MemoryStore()
        with patch.object(telemetry_module, "Store", return_value=store):
            manager = TelemetryManager(hass)
        return hass, manager

    async def test_defaults_off_and_saving_off_never_contacts_receiver(self):
        hass, manager = self.make_manager()
        with patch.object(telemetry_module, "ClientSession", FakeSession):
            await manager.async_setup()
            self.assertEqual(manager.data["preferences"], {
                "base": False, "usage": False, "statistics": False, "diagnostics": False})
            self.assertRegex(manager.data["installation_id"], r"^[a-f0-9]{32}$")
            await manager.async_set_preferences(dict(manager.data["preferences"]))
        self.assertEqual(FakeSession.calls, [])
        self.assertEqual(hass.bus.callback.__self__, manager)
        await manager.async_close()

    async def test_enabled_categories_send_exact_filtered_contract(self):
        hass, manager = self.make_manager()
        hass.config.components = {"sensor", "light", "bad-domain", "hass"}
        device = SimpleNamespace(
            manufacturer="Acme", model="Model X", name_by_user="Kitchen", name="Hub")
        unsafe_device = SimpleNamespace(
            manufacturer="10.0.0.5", model="Model Z", name_by_user=None, name=None)
        registry = SimpleNamespace(devices=[device, device, unsafe_device])
        entity_registry = SimpleNamespace(entities=["one", "two", "three"])
        hass.states.async_entity_ids = lambda domain: ["automation.one"] if domain == "automation" else []
        hass.auth.async_get_users = AsyncMock(return_value=["owner", "guest"])
        with (
            patch.object(telemetry_module, "Store", return_value=manager.store),
            patch.object(telemetry_module, "ClientSession", FakeSession),
            patch.object(telemetry_module, "async_track_time_interval", return_value=lambda: None),
            patch.object(telemetry_module.device_registry, "async_get", return_value=registry),
            patch.object(telemetry_module.entity_registry, "async_get", return_value=entity_registry),
            patch.object(telemetry_module, "async_get_system_info", new=AsyncMock(return_value={
                "installation_type": "Home Assistant Container", "arch": "x86_64",
                "python_version": "3.14.2", "timezone": "Private/Timezone", "user": "private-user",
            })),
        ):
            await manager.async_setup()
            state = await manager.async_set_preferences({
                "base": True, "usage": True, "statistics": True, "diagnostics": False})

        self.assertIsNotNone(state["last_delivery"])
        self.assertIsNone(state["last_error"])
        self.assertEqual(len(FakeSession.calls), 2)
        preferences_body = json.loads(FakeSession.calls[0][1]["data"])
        analytics_body = json.loads(FakeSession.calls[1][1]["data"])
        self.assertEqual(FakeSession.calls[0][0], telemetry_module.RECEIVER_URL)
        self.assertFalse(FakeSession.calls[0][1]["allow_redirects"])
        self.assertIsInstance(FakeSession.options[0]["cookie_jar"], DummyCookieJar)
        self.assertEqual(FakeSession.options[0]["timeout"].total, 10)
        self.assertLessEqual(len(FakeSession.calls[0][1]["data"]), telemetry_module.MAX_BODY_BYTES)
        self.assertEqual(set(preferences_body), {"schema", "installation_id", "kind", "preferences", "data"})
        self.assertEqual(preferences_body["kind"], "preferences")
        self.assertEqual(preferences_body["data"], {})
        self.assertEqual(analytics_body["kind"], "analytics")
        self.assertEqual(set(analytics_body["data"]), {"base", "usage", "statistics"})
        self.assertEqual(set(analytics_body["data"]["base"]), {
            "ha_version", "habab_version", "installation_type", "arch", "python_version"})
        self.assertEqual(analytics_body["data"]["usage"]["integrations"], ["hass", "light", "sensor"])
        self.assertEqual(analytics_body["data"]["usage"]["device_models"], [
            {"manufacturer": "Acme", "model": "Model X", "count": 2}])
        self.assertEqual(analytics_body["data"]["statistics"], {
            "devices": 3, "entities": 3, "automations": 1, "users": 2})
        serialized = json.dumps(analytics_body)
        for private_value in ("Private/Timezone", "private-user", "Kitchen", "10.0.0.5"):
            self.assertNotIn(private_value, serialized)
        await manager.async_close()

    def test_diagnostic_contains_only_exception_type_component_and_safe_frames(self):
        try:
            private_local_value = "fixture-local-secret"
            raise RuntimeError("fixture-message-secret")
        except RuntimeError:
            record = logging.LogRecord(
                "homeassistant.components.habab_home.private", logging.ERROR,
                "/private/home/path/private_file.py", 42,
                "fixture-message-secret %s", (private_local_value,), None)
            record.exc_info = sys.exc_info()
        diagnostic = _clean_diagnostic(record)
        self.assertEqual(set(diagnostic), {"error_type", "component", "frames"})
        self.assertEqual(diagnostic["error_type"], "RuntimeError")
        self.assertEqual(diagnostic["component"], "habab_home")
        self.assertTrue(all(set(frame) == {"file", "function", "line"} for frame in diagnostic["frames"]))
        self.assertTrue(all("/" not in frame["file"] for frame in diagnostic["frames"]))
        self.assertLessEqual(len(diagnostic["frames"]), 12)
        serialized = json.dumps(diagnostic)
        self.assertNotIn("fixture-message-secret", serialized)
        self.assertNotIn("fixture-local-secret", serialized)
        self.assertNotIn("/private/home/path", serialized)

    async def test_diagnostics_are_independent_bounded_and_disabled_without_consent(self):
        _, manager = self.make_manager()
        manager.data["preferences"] = {
            "base": False, "usage": False, "statistics": False, "diagnostics": True}
        manager.data["ever_opted_in"] = True
        event = {"error_type": "RuntimeError", "component": "core", "frames": []}
        with patch.object(telemetry_module, "ClientSession", FakeSession):
            for _ in range(25):
                await manager.async_enqueue_diagnostic(event)
            if manager._diagnostic_worker:
                await manager._diagnostic_worker
            self.assertEqual(len(FakeSession.calls), 20)
            body = json.loads(FakeSession.calls[0][1]["data"])
            self.assertEqual(body["kind"], "diagnostic")
            self.assertFalse(body["preferences"]["base"])
            self.assertTrue(body["preferences"]["diagnostics"])
            self.assertEqual(body["data"], event)

            await manager.async_set_preferences({
                "base": False, "usage": False, "statistics": False, "diagnostics": False})
            sends_after_withdrawal = len(FakeSession.calls)
            await manager.async_enqueue_diagnostic(event)
            self.assertEqual(len(FakeSession.calls), sends_after_withdrawal)
            self.assertEqual(json.loads(FakeSession.calls[-1][1]["data"])["kind"], "withdraw")
        await manager.async_close()

    async def test_repeat_analytics_is_rate_limited_locally(self):
        _, manager = self.make_manager()
        manager.data["preferences"] = {
            "base": True, "usage": False, "statistics": False, "diagnostics": False}
        manager.data["last_analytics_attempt"] = datetime.now(timezone.utc).isoformat()
        with patch.object(telemetry_module, "ClientSession", FakeSession):
            await manager.async_send_analytics()
        self.assertEqual(FakeSession.calls, [])
        self.assertEqual(manager.data["last_error"], "rate_limited")

    async def test_usage_consent_requires_basic_category(self):
        _, manager = self.make_manager()
        with patch.object(telemetry_module, "ClientSession", FakeSession):
            with self.assertRaisesRegex(ValueError, "base_required"):
                await manager.async_set_preferences({
                    "base": False, "usage": True, "statistics": False, "diagnostics": False})
        self.assertEqual(FakeSession.calls, [])
        self.assertFalse(any(manager.data["preferences"].values()))


if __name__ == "__main__":
    unittest.main()
