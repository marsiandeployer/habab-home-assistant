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
from homeassistant.components.habab_home.setup_actions import execute, flow_result
from homeassistant.components.habab_home.const import DOMAIN


class ActionBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_assistant_status_reports_voice_capabilities_without_inventing_client_observation(self):
        for provider, supported in (("deepseek", False), ("openai", True), ("habab", True), ("gemini", True)):
            settings = {"provider": provider, "api_key": "fixture-api-secret", "model": "configured-text-model"}
            telegram = SimpleNamespace(status=AsyncMock(return_value={
                "phase": "connected", "pairing_url": "fixture-pairing-secret",
                "group": {"phase": "awaiting_admin", "chat_id": "fixture-private-chat"}}))
            hass = SimpleNamespace(data={
                DOMAIN: {"setup_agent": SimpleNamespace(settings=lambda: settings)},
                "habab_telegram": telegram})
            result = await execute(hass, None, "alice", {"action": "assistant_status"})
            self.assertEqual(result["selected_provider"], provider)
            self.assertEqual(result["selected_text_model"], "configured-text-model")
            if provider == "gemini":
                self.assertEqual(result["voice"]["required_client_kind"], "native_v2")
                self.assertEqual(result["voice"]["selected_provider_protocol"], "gemini_live")
                self.assertTrue(result["voice"]["default_voice_model"])
            self.assertEqual(result["voice"]["selected_provider_supports_realtime"], supported)
            self.assertIs(result["voice"]["supported_clients"]["android_with_native_voice"]["requires_ha_stt_tts"], False)
            self.assertIs(result["voice"]["supported_clients"]["android_with_native_voice"]["custom_sound_selection"], False)
            self.assertEqual(result["voice"]["observed_client"]["outbound_audio"], "not_observed")
            self.assertEqual(result["telegram"]["group_phase"], "awaiting_admin")
            for secret in ("fixture-api-secret", "fixture-pairing-secret", "fixture-private-chat"):
                self.assertNotIn(secret, json.dumps(result))
            telegram.status.assert_awaited_once_with("alice")

    def test_flow_result_never_returns_entry_credentials_or_defaults(self):
        result = flow_result({"type": "create_entry", "flow_id": "flow-one", "title": "Private",
            "data": {"api_key": "fixture-private-key"}, "result": object(),
            "description_placeholders": {"token": "fixture-private-token"}})
        self.assertEqual(result, {"phase": "progress", "flow_id": "flow-one"})

    def test_flow_result_never_exposes_generic_form_schema_or_external_urls(self):
        result = flow_result({"type": "form", "flow_id": "flow-one", "step_id": "user",
            "data_schema": {"api_key": "fixture-schema-secret"},
            "data": {"api_key": "fixture-entry-secret"},
            "description_placeholders": {"secret": "fixture-placeholder-secret"},
            "url": "https://example.invalid/oauth?state=fixture-url-secret"})
        self.assertEqual(result, {"phase": "form", "flow_id": "flow-one", "step_id": "user"})
        serialized = json.dumps(result)
        for secret in ("fixture-schema-secret", "fixture-entry-secret", "fixture-placeholder-secret", "fixture-url-secret"):
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
        result = {"interaction_id": "interaction-one", "flow_id": "discover-one", "phase": "form"}
        controller = SimpleNamespace(agent_action=AsyncMock(return_value=result))
        hass = SimpleNamespace(data={DOMAIN: {"integration_setup": controller}})
        manager = SimpleNamespace()
        actual = await execute(hass, manager, "alice", {
            "action": "adopt_discovered_flow", "flow_id": "discover-one"})
        self.assertEqual(actual, result)
        controller.agent_action.assert_awaited_once_with("alice", "adopt_discovered_flow", {
            "action": "adopt_discovered_flow", "flow_id": "discover-one"})

    async def test_model_body_cannot_supply_completion_origin(self):
        controller = SimpleNamespace(agent_action=AsyncMock(return_value={"phase": "form"}))
        hass = SimpleNamespace(data={DOMAIN: {"integration_setup": controller}})
        forged = {"channel": "telegram", "owner": "mallory", "entry_id": "other", "chat_id": "9"}
        await execute(hass, None, "alice", {
            "action": "start_integration", "domain": "fixture", "trusted_origin": forged,
        })
        controller.agent_action.assert_awaited_once_with("alice", "start_integration", {
            "action": "start_integration", "domain": "fixture", "trusted_origin": forged,
        })

    async def test_another_owner_cannot_continue_a_configuration_flow(self):
        controller = SimpleNamespace(agent_action=AsyncMock(side_effect=ValueError("unknown_flow")))
        hass = SimpleNamespace(data={DOMAIN: {"integration_setup": controller}})
        with self.assertRaisesRegex(ValueError, "unknown_flow"):
            await execute(hass, None, "bob", {
                "action": "continue_integration", "flow_id": "flow-one", "input": {}})
        controller.agent_action.assert_awaited_once()

    async def test_unknown_action_is_rejected(self):
        with self.assertRaises(ValueError):
            await execute(None, None, "alice", {"action": "shell", "command": "anything"})


if __name__ == "__main__":
    unittest.main()
