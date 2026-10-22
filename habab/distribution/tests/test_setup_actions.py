"""Native action bridge: flow ownership and secret-free form serialization."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from homeassistant.helpers.device_registry import (
    ActiveDeviceRegistryItems,
    DeviceEntry,
    DeviceRegistry,
    _DeprecatedDeviceRegistryItemsView,
)
from homeassistant.helpers.selector import SelectSelector
from homeassistant.components.habab_home.setup_actions import execute, flow_result
from probatio import Required


class ActionBridgeTests(unittest.IsolatedAsyncioTestCase):
    def test_flow_result_never_returns_entry_credentials_or_defaults(self):
        result = flow_result({"type": "create_entry", "flow_id": "flow-one", "title": "Private",
            "data": {"api_key": "fixture-private-key"}, "result": object(),
            "description_placeholders": {"token": "fixture-private-token"}})
        self.assertEqual(result, {"type": "create_entry", "flow_id": "flow-one", "fields": []})

    def test_flow_result_exposes_field_types_and_select_options_without_defaults(self):
        result = flow_result({"type": "form", "flow_id": "flow-one", "step_id": "user",
            "data_schema": SimpleNamespace(schema={
                Required("api_key", default="fixture-schema-secret"): str,
                "transport": SelectSelector({"options": ["serial", "wifi"]}),
            }),
            "data": {"api_key": "fixture-entry-secret"},
            "description_placeholders": {"secret": "fixture-placeholder-secret"}})
        self.assertEqual(result["fields"], [
            {"name": "api_key", "type": "str"},
            {"name": "transport", "type": "select", "options": ["serial", "wifi"]},
        ])
        serialized = json.dumps(result)
        for secret in ("fixture-schema-secret", "fixture-entry-secret", "fixture-placeholder-secret"):
            self.assertNotIn(secret, serialized)

    async def test_devices_uses_iterable_device_entries_and_resolves_entities(self):
        device = DeviceEntry(config_entry_id="entry-id", id="device-id",
            identifiers={("test", "device")}, name="Sensor", name_by_user="Kitchen",
            model="Model X", manufacturer="Maker", area_id="kitchen")
        items = ActiveDeviceRegistryItems()
        items[device.id] = device
        device_registry = object.__new__(DeviceRegistry)
        device_registry._devices = items
        device_registry.devices = _DeprecatedDeviceRegistryItemsView(items)
        # Lookup without identifiers returns no matches; it is not enumeration.
        self.assertEqual(device_registry.async_get_devices(), [])
        entity_registry = object()
        entries = [SimpleNamespace(entity_id="sensor.kitchen_temperature")]
        with patch("homeassistant.components.habab_home.setup_actions.device_registry.async_get",
                   return_value=device_registry), \
             patch("homeassistant.components.habab_home.setup_actions.entity_registry.async_get",
                   return_value=entity_registry), \
             patch("homeassistant.components.habab_home.setup_actions.entity_registry.async_entries_for_device",
                   return_value=entries) as entries_for_device:
            result = await execute(None, None, "alice", {"action": "devices"})
        entries_for_device.assert_called_once_with(entity_registry, "device-id")
        self.assertEqual(result, {"devices": [{"id": "device-id", "name": "Kitchen", "model": "Model X",
            "manufacturer": "Maker", "area_id": "kitchen", "entities": ["sensor.kitchen_temperature"]}]})
        json.dumps(result)

    async def test_discovered_flows_lists_only_unowned_native_discovery_summaries(self):
        flow_results = [
            {"flow_id": "discover-one", "handler": "test_integration", "step_id": "user",
             "title": "Found device", "menu_options": ["configure"],
             "context": {"source": "bluetooth", "title_placeholders": {"name": "Device"},
                         "secret": "fixture-flow-secret"}},
            {"flow_id": "user-flow", "handler": "test_integration", "context": {"source": "user"}},
            {"flow_id": "reauth-flow", "handler": "test_integration", "context": {"source": "reauth"}},
            {"flow_id": "owned-flow", "handler": "test_integration", "context": {"source": "dhcp"}},
        ]
        hass = SimpleNamespace(config_entries=SimpleNamespace(flow=SimpleNamespace(
            async_progress=lambda: flow_results)))
        manager = SimpleNamespace(flow_owners={"owned-flow": ("bob", "config")})
        result = await execute(hass, manager, "alice", {"action": "discovered_flows"})
        self.assertEqual(result, {"flows": [{"flow_id": "discover-one", "domain": "test_integration",
            "step_id": "user", "title": "Found device", "menu_options": ["configure"]}]})
        self.assertNotIn("fixture-flow-secret", json.dumps(result))

    async def test_owner_must_explicitly_adopt_unowned_discovered_flow(self):
        flow = {"flow_id": "discover-one", "handler": "test_integration", "type": "form",
            "step_id": "user", "context": {"source": "zeroconf", "secret": "fixture-secret"},
            "data": {"password": "fixture-entry-secret"}}
        flow_results = [flow,
            {"flow_id": "foreign-flow", "context": {"source": "dhcp"}},
            {"flow_id": "user-flow", "context": {"source": "user"}}]
        hass = SimpleNamespace(config_entries=SimpleNamespace(flow=SimpleNamespace(
            async_progress=lambda: flow_results)))
        manager = SimpleNamespace(flow_owners={})
        result = await execute(hass, manager, "alice", {
            "action": "adopt_discovered_flow", "flow_id": "discover-one"})
        self.assertEqual(manager.flow_owners, {"discover-one": ("alice", "config")})
        self.assertEqual(result, {"type": "form", "flow_id": "discover-one", "step_id": "user", "fields": []})
        self.assertNotIn("fixture-secret", json.dumps(result))
        self.assertNotIn("fixture-entry-secret", json.dumps(result))

        with self.assertRaises(ValueError):
            await execute(hass, manager, "bob", {
                "action": "adopt_discovered_flow", "flow_id": "discover-one"})
        self.assertEqual(manager.flow_owners, {"discover-one": ("alice", "config")})

        manager.flow_owners["foreign-flow"] = ("bob", "config")
        for flow_id in ("foreign-flow", "user-flow"):
            with self.subTest(flow_id=flow_id), self.assertRaises(ValueError):
                await execute(hass, manager, "alice", {
                    "action": "adopt_discovered_flow", "flow_id": flow_id})
        self.assertEqual(manager.flow_owners, {
            "discover-one": ("alice", "config"), "foreign-flow": ("bob", "config")})

    async def test_another_owner_cannot_continue_a_configuration_flow(self):
        configure = AsyncMock()
        hass = SimpleNamespace(config_entries=SimpleNamespace(flow=SimpleNamespace(async_configure=configure)))
        manager = SimpleNamespace(flow_owners={"flow-one": ("alice", "config")})
        with self.assertRaises(ValueError):
            await execute(hass, manager, "bob", {"action": "continue_integration", "flow_id": "flow-one", "input": {}})
        configure.assert_not_awaited()

    async def test_unknown_action_is_rejected(self):
        with self.assertRaises(ValueError):
            await execute(None, None, "alice", {"action": "shell", "command": "anything"})
