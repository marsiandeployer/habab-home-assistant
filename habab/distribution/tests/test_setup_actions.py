"""Native action bridge: flow ownership and secret-free form serialization."""
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.homeassistant import async_setup as async_setup_homeassistant
from homeassistant.components.homeassistant import exposed_entities
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry, device_registry, entity_registry
from homeassistant.helpers.translation import async_get_translations
from homeassistant.loader import (
    IntegrationNotFound,
    async_setup as async_setup_loader,
    async_get_integration,
    async_get_config_flows,
    async_get_integration_descriptions,
)
from homeassistant.helpers.device_registry import (
    ActiveDeviceRegistryItems,
    DeviceEntry,
    DeviceRegistry,
    DeviceEntryType,
    _DeprecatedDeviceRegistryItemsView,
)
from homeassistant.components.habab_home.setup_actions import (
    AssistExposureError,
    SetupActionsView,
    _catalog_language,
    execute,
    flow_result,
)
from homeassistant.components.habab_home.const import DOMAIN
from homeassistant.components.habab_home.setup_agent import PROMPT
from homeassistant.components.habab_home.voice_context import build_voice_context


class ActionBridgeTests(unittest.IsolatedAsyncioTestCase):
    def test_catalog_language_rejects_path_fragments(self):
        self.assertEqual(
            _catalog_language(SimpleNamespace(config=SimpleNamespace(language="../../private"))),
            "en",
        )

    async def test_integration_catalog_uses_native_flow_metadata_and_hass_locale(self):
        hass = SimpleNamespace(
            config=SimpleNamespace(language="ru-RU"),
            config_entries=SimpleNamespace(async_entries=lambda: [
                SimpleNamespace(domain="met"),
            ]),
        )
        flow_domains = {"met", "custom_weather", "counter", "missing_metadata", "no_flow"}
        descriptions = {
            "core": {
                "integration": {
                    "met": {"name": "Met.no", "config_flow": True, "integration_type": "service"},
                    "no_flow": {"name": "Legacy", "config_flow": False, "integration_type": "hub"},
                },
                "helper": {"counter": {"name": "Counter", "config_flow": True}},
            },
            "custom": {
                "integration": {
                    "custom_weather": {"name": "Custom weather", "config_flow": True, "integration_type": "hub"},
                },
                "helper": {},
            },
        }
        translations = {
            "component.met.title": "Метеорологический институт",
            "component.custom_weather.title": "Пользовательская погода",
        }
        with patch(
            "homeassistant.components.habab_home.setup_actions.async_get_config_flows",
            AsyncMock(return_value=flow_domains),
        ) as get_flows, patch(
            "homeassistant.components.habab_home.setup_actions.async_get_integration_descriptions",
            AsyncMock(return_value=descriptions),
        ) as get_descriptions, patch(
            "homeassistant.components.habab_home.setup_actions.async_get_integration",
            AsyncMock(side_effect=IntegrationNotFound("fixture_missing")),
        ), patch(
            "homeassistant.components.habab_home.setup_actions.async_get_translations",
            AsyncMock(return_value=translations),
        ) as get_translations:
            result = await execute(hass, None, "owner", {
                "action": "integration_catalog",
                "offset": 0,
                "limit": 2,
                # The locale comes from Home Assistant configuration, not the model body.
                "language": "../../private",
            })

        self.assertEqual(result, {
            "integrations": [
                {"domain": "custom_weather", "name": "Пользовательская погода",
                 "integration_type": "hub", "configured": False},
                {"domain": "met", "name": "Метеорологический институт",
                 "integration_type": "service", "configured": True},
            ],
            "offset": 0,
            "limit": 2,
            "total": 2,
            "next_offset": None,
        })
        get_flows.assert_awaited_once_with(hass)
        get_descriptions.assert_awaited_once_with(hass)
        get_translations.assert_awaited_once_with(
            hass, "ru-RU", "title", integrations=["custom_weather", "met"]
        )
        serialized = json.dumps(result, ensure_ascii=False)
        for excluded in (
            "counter", "missing_metadata", "no_flow", "habab_home",
            "habab_telegram", "habab_yandex_speech", "../../private",
        ):
            self.assertNotIn(excluded, serialized)

    async def test_integration_catalog_bounds_pages_and_rejects_boolean_numbers(self):
        domains = {f"integration_{index:03d}" for index in range(101)}
        descriptions = {
            "core": {
                "integration": {
                    domain: {"name": domain, "config_flow": True, "integration_type": "hub"}
                    for domain in domains
                },
                "helper": {},
            },
            "custom": {"integration": {}, "helper": {}},
        }
        hass = SimpleNamespace(
            config=SimpleNamespace(language="en"),
            config_entries=SimpleNamespace(async_entries=lambda: []),
        )
        with patch(
            "homeassistant.components.habab_home.setup_actions.async_get_config_flows",
            AsyncMock(return_value=domains),
        ), patch(
            "homeassistant.components.habab_home.setup_actions.async_get_integration_descriptions",
            AsyncMock(return_value=descriptions),
        ), patch(
            "homeassistant.components.habab_home.setup_actions.async_get_integration",
            AsyncMock(side_effect=IntegrationNotFound("fixture_missing")),
        ), patch(
            "homeassistant.components.habab_home.setup_actions.async_get_translations",
            AsyncMock(return_value={}),
        ):
            page = await execute(hass, None, "owner", {
                "action": "integration_catalog", "offset": 100, "limit": 1,
            })
            self.assertEqual(page["total"], 101)
            self.assertEqual(page["integrations"], [{
                "domain": "integration_100", "name": "integration_100",
                "integration_type": "hub", "configured": False,
            }])
            self.assertIsNone(page["next_offset"])
            for body in (
                {"action": "integration_catalog", "offset": True},
                {"action": "integration_catalog", "limit": False},
                {"action": "integration_catalog", "limit": 101},
                {"action": "integration_catalog", "offset": 10_001},
            ):
                with self.subTest(body=body), self.assertRaises(ValueError):
                    await execute(hass, None, "owner", body)

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

    async def test_xiaomi_handoff_describes_the_authenticated_channel_without_exposing_secrets(self):
        xiaomi = SimpleNamespace(request=AsyncMock(return_value={"phase": "oauth", "region": "ru"}))
        hass = SimpleNamespace(data={DOMAIN: {"xiaomi_setup": xiaomi}})
        origin = {"channel": "telegram", "owner": "alice", "entry_id": "bot", "chat_id": "42", "bot_id": 99}
        telegram = await execute(hass, None, "alice", {"action": "xiaomi_setup"}, trusted_origin=origin)
        xiaomi.request.assert_awaited_once_with("alice", origin)
        self.assertEqual(telegram["setup"], {"phase": "oauth", "region": "ru"})
        self.assertIn("available only in the Home Assistant web chat", telegram["instruction"])
        self.assertIn("Do not claim", telegram["instruction"])
        self.assertIn("never chat messages", telegram["instruction"])
        xiaomi.request.reset_mock()
        web = await execute(hass, None, "alice", {"action": "xiaomi_setup", "trusted_origin": origin})
        xiaomi.request.assert_awaited_once_with("alice")
        self.assertIn("shown inside this Home Assistant web chat", web["instruction"])
        self.assertNotIn("ask the user to open", web["instruction"])

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
            model="Model X", manufacturer="Maker", area_id="kitchen",
            entry_type=DeviceEntryType.SERVICE)
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
            hass = SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda: [
                SimpleNamespace(entry_id="entry-id", domain="test_domain")]))
            result = await execute(hass, None, "alice", {"action": "devices"})
        entries_for_device.assert_called_once_with(entity_registry, "device-id")
        self.assertEqual(result, {
            "devices": [{"id": "device-id", "name": "Kitchen", "model": "Model X",
                "manufacturer": "Maker", "area_id": "kitchen", "entry_type": "service",
                "entry_type_description": "служебная запись",
                "integration_domain": "test_domain", "entities": ["sensor.kitchen_temperature"]}],
            "count": 1, "total_count": 1, "limit": 200, "truncated": False,
        })
        json.dumps(result)

    async def test_devices_leave_missing_entry_type_unknown(self):
        device = DeviceEntry(config_entry_id="entry-id", id="device-id",
                             identifiers={("test", "device")}, entry_type=None)
        items = ActiveDeviceRegistryItems()
        items[device.id] = device
        registry = object.__new__(DeviceRegistry)
        registry._devices = items
        registry.devices = _DeprecatedDeviceRegistryItemsView(items)
        with patch("homeassistant.components.habab_home.setup_actions.device_registry.async_get",
                   return_value=registry), \
             patch("homeassistant.components.habab_home.setup_actions.entity_registry.async_get",
                   return_value=object()), \
             patch("homeassistant.components.habab_home.setup_actions.entity_registry.async_entries_for_device",
                   return_value=[]):
            hass = SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda: [
                SimpleNamespace(entry_id="entry-id", domain="fixture")]))
            result = await execute(hass, None, "alice", {"action": "devices"})
        self.assertIsNone(result["devices"][0]["entry_type"])
        self.assertIsNone(result["devices"][0]["entry_type_description"])

    async def test_devices_count_and_truncation_describe_full_registry(self):
        item_count = 201
        items = ActiveDeviceRegistryItems()
        config_entries = []
        for index in range(item_count):
            entry_id = f"entry-{index}"
            device = DeviceEntry(config_entry_id=entry_id, id=f"device-{index}",
                                 identifiers={("test", str(index))})
            items[device.id] = device
            config_entries.append(SimpleNamespace(entry_id=entry_id, domain="fixture"))
        registry = object.__new__(DeviceRegistry)
        registry._devices = items
        registry.devices = _DeprecatedDeviceRegistryItemsView(items)
        entity_registry = object()
        with patch("homeassistant.components.habab_home.setup_actions.device_registry.async_get",
                   return_value=registry), \
             patch("homeassistant.components.habab_home.setup_actions.entity_registry.async_get",
                   return_value=entity_registry), \
             patch("homeassistant.components.habab_home.setup_actions.entity_registry.async_entries_for_device",
                   return_value=[]):
            hass = SimpleNamespace(config_entries=SimpleNamespace(
                async_entries=lambda: config_entries))
            result = await execute(hass, None, "alice", {"action": "devices"})
        self.assertEqual(result["count"], 200)
        self.assertEqual(result["total_count"], item_count)
        self.assertEqual(result["limit"], 200)
        self.assertIs(result["truncated"], True)
        self.assertEqual(len(result["devices"]), 200)
        self.assertEqual(result["devices"][0]["integration_domain"], "fixture")
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

    def test_prompt_only_changes_exact_assist_exposure_on_explicit_request(self):
        self.assertIn('"action":"assist_exposure","operation":"get"', PROMPT)
        self.assertIn('"action":"assist_exposure","operation":"set"', PROMPT)
        self.assertIn('Не меняй экспозицию Assist ради ответа на обычный запрос о погоде', PROMPT)
        self.assertIn('Меняй экспозицию только по явной просьбе владельца', PROMPT)
        self.assertIn('не делай массовые изменения', PROMPT)
        self.assertIn('не меняй настройки Alexa, Google или другого assistant', PROMPT)
        self.assertIn('проверь effective в ответе', PROMPT)

    async def test_assist_exposure_requires_existing_entity_and_strict_boolean(self):
        hass = SimpleNamespace(states=SimpleNamespace(
            get=lambda entity_id: SimpleNamespace(entity_id=entity_id)
            if entity_id == "weather.fixture" else None))
        with patch("homeassistant.components.habab_home.setup_actions._assist_exposure_api") as api:
            with self.assertRaisesRegex(AssistExposureError, "assist_entity_not_found"):
                await execute(hass, None, "alice", {"action": "assist_exposure",
                    "operation": "set", "entity_id": "weather.missing", "should_expose": True})
            for value in (1, 0, "true", None):
                with self.subTest(value=value), self.assertRaisesRegex(
                    AssistExposureError, "assist_exposure_boolean_required"
                ):
                    await execute(hass, None, "alice", {"action": "assist_exposure",
                        "operation": "set", "entity_id": "weather.fixture", "should_expose": value})
            with self.assertRaisesRegex(AssistExposureError, "assist_exposure_operation_invalid"):
                await execute(hass, None, "alice", {"action": "assist_exposure",
                    "operation": [], "entity_id": "weather.fixture"})
            api.assert_not_called()

    async def test_assist_exposure_helper_errors_and_false_readback_fail_explicitly(self):
        hass = SimpleNamespace(states=SimpleNamespace(
            get=lambda entity_id: SimpleNamespace(entity_id=entity_id)))
        body = {"action": "assist_exposure", "operation": "set",
                "entity_id": "weather.fixture", "should_expose": True}
        with patch("homeassistant.components.habab_home.setup_actions._assist_exposure_api",
                   side_effect=AssistExposureError("assist_exposure_unavailable")):
            with self.assertRaisesRegex(AssistExposureError, "assist_exposure_unavailable"):
                await execute(hass, None, "alice", body)
        helper_error = SimpleNamespace(async_should_expose=Mock(side_effect=RuntimeError()),
                                       async_expose_entity=AsyncMock())
        with patch("homeassistant.components.habab_home.setup_actions._assist_exposure_api",
                   return_value=helper_error):
            with self.assertRaisesRegex(AssistExposureError, "assist_exposure_set_readback_failed"):
                await execute(hass, None, "alice", body)
        mismatch = SimpleNamespace(async_should_expose=Mock(side_effect=[False, False]),
                                   async_expose_entity=Mock())
        with patch("homeassistant.components.habab_home.setup_actions._assist_exposure_api",
                   return_value=mismatch):
            with self.assertRaisesRegex(AssistExposureError, "assist_exposure_readback_mismatch"):
                await execute(hass, None, "alice", body)

    async def test_setup_actions_capability_still_rejects_non_admin_owners(self):
        class Content:
            async def iter_chunked(self, size):
                yield b'{"action":"assist_exposure","operation":"get","entity_id":"weather.fixture"}'

        for user in (SimpleNamespace(is_active=False, is_admin=True),
                     SimpleNamespace(is_active=True, is_admin=False)):
            manager = SimpleNamespace(active=("alice", "interaction"), control_token="fixture-token")
            assistant = SimpleNamespace(trusted_origin=AsyncMock(return_value=None))
            hass = SimpleNamespace(
                data={DOMAIN: {"setup_agent": manager, "assistant": assistant}},
                auth=SimpleNamespace(async_get_user=AsyncMock(return_value=user)),
            )
            request = SimpleNamespace(
                app={"hass": hass}, headers={"X-Habab-Agent-Token": "fixture-token"},
                remote="127.0.0.1", content=Content(),
            )
            with patch("homeassistant.components.habab_home.setup_actions._assist_exposure_api") as api:
                response = await SetupActionsView().post(request)
            self.assertEqual(response.status, 403)
            api.assert_not_called()
            assistant.trusted_origin.assert_not_awaited()

    async def test_setup_actions_view_returns_explicit_native_helper_failure(self):
        class Content:
            async def iter_chunked(self, size):
                yield b'{"action":"assist_exposure","operation":"get","entity_id":"weather.fixture"}'

        user = SimpleNamespace(is_active=True, is_admin=True)
        manager = SimpleNamespace(active=("alice", "interaction"), control_token="fixture-token")
        assistant = SimpleNamespace(trusted_origin=AsyncMock(return_value=None))
        hass = SimpleNamespace(
            data={DOMAIN: {"setup_agent": manager, "assistant": assistant}},
            auth=SimpleNamespace(async_get_user=AsyncMock(return_value=user)),
            states=SimpleNamespace(get=lambda entity_id: SimpleNamespace(entity_id=entity_id)),
        )
        request = SimpleNamespace(
            app={"hass": hass}, headers={"X-Habab-Agent-Token": "fixture-token"},
            remote="127.0.0.1", content=Content(),
        )
        with patch("homeassistant.components.habab_home.setup_actions._assist_exposure_api",
                   side_effect=AssistExposureError("assist_exposure_unavailable")):
            response = await SetupActionsView().post(request)
        self.assertEqual(response.status, 503)
        self.assertEqual(json.loads(response.body), {"error": "assist_exposure_unavailable"})


class NativeAssistExposureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config_dir = tempfile.TemporaryDirectory(prefix="habab-assist-exposure-")
        self.addCleanup(self.config_dir.cleanup)
        self.hass = HomeAssistant(self.config_dir.name)
        await self.hass.async_start()
        self.addAsyncCleanup(self.hass.async_stop, force=True)
        await async_setup_homeassistant(self.hass, {})
        device_registry.async_setup(self.hass)
        await device_registry.async_load(self.hass)
        await area_registry.async_load(self.hass)
        await entity_registry.async_load(self.hass)
        registry = entity_registry.async_get(self.hass)
        entry = registry.async_get_or_create(
            "weather", "assist_exposure_fixture", "forecast",
            suggested_object_id="assist_exposure_forecast",
            original_name="Synthetic forecast",
        )
        self.entity_id = entry.entity_id
        self.hass.states.async_set(self.entity_id, "partlycloudy", {
            "temperature": 9.1, "temperature_unit": "°C",
        })
        exposed_entities.async_expose_entity(
            self.hass, "fixture_other_assistant", self.entity_id, False
        )

    async def test_real_core_helper_sets_exact_entity_and_reads_back_without_other_assistant_changes(self):
        initial_snapshot = build_voice_context(self.hass)
        self.assertEqual(initial_snapshot["weather"], {"status": "unavailable", "entities": []})
        before = await execute(self.hass, None, "alice", {
            "action": "assist_exposure", "operation": "get", "entity_id": self.entity_id,
        })
        self.assertFalse(before["effective"])
        changed = await execute(self.hass, None, "alice", {
            "action": "assist_exposure", "operation": "set", "entity_id": self.entity_id,
            "should_expose": True,
        })
        self.assertEqual(changed, {
            "entity_id": self.entity_id,
            "assistant": "conversation",
            "before": False,
            "requested": True,
            "effective": True,
            "changed": True,
            "verification": "native_readback",
            "storage": "entity_registry",
        })
        registry_entry = entity_registry.async_get(self.hass).async_get(self.entity_id)
        self.assertIs(registry_entry.options["conversation"]["should_expose"], True)
        self.assertIs(registry_entry.options["fixture_other_assistant"]["should_expose"], False)
        self.assertTrue(exposed_entities.async_should_expose(self.hass, "conversation", self.entity_id))
        self.assertFalse(exposed_entities.async_should_expose(
            self.hass, "fixture_other_assistant", self.entity_id
        ))
        enabled_snapshot = build_voice_context(self.hass)
        self.assertEqual(enabled_snapshot["weather"]["status"], "available")
        self.assertEqual(enabled_snapshot["weather"]["entities"][0]["entity_id"], self.entity_id)
        self.assertEqual(enabled_snapshot["weather"]["entities"][0]["temperature"], 9.1)

        after_get = await execute(self.hass, None, "alice", {
            "action": "assist_exposure", "operation": "get", "entity_id": self.entity_id,
        })
        self.assertTrue(after_get["effective"])
        disabled = await execute(self.hass, None, "alice", {
            "action": "assist_exposure", "operation": "set", "entity_id": self.entity_id,
            "should_expose": False,
        })
        self.assertEqual(disabled["before"], True)
        self.assertEqual(disabled["requested"], False)
        self.assertEqual(disabled["effective"], False)
        self.assertTrue(disabled["changed"])
        idempotent = await execute(self.hass, None, "alice", {
            "action": "assist_exposure", "operation": "set", "entity_id": self.entity_id,
            "should_expose": False,
        })
        self.assertFalse(idempotent["changed"])
        self.assertEqual(idempotent["effective"], False)
        registry_entry = entity_registry.async_get(self.hass).async_get(self.entity_id)
        self.assertIs(registry_entry.options["conversation"]["should_expose"], False)
        self.assertIs(registry_entry.options["fixture_other_assistant"]["should_expose"], False)


class NativeIntegrationCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config_dir = tempfile.TemporaryDirectory(prefix="habab-integration-catalog-")
        self.addCleanup(self.config_dir.cleanup)
        self.hass = HomeAssistant(self.config_dir.name)
        async_setup_loader(self.hass)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        await self.hass.async_start()
        self.addAsyncCleanup(self.hass.async_stop, force=True)

    async def test_catalog_matches_real_core_config_flow_catalog_and_titles(self):
        flow_domains = await async_get_config_flows(self.hass)
        descriptions = await async_get_integration_descriptions(self.hass)
        integration_metadata = descriptions["core"]["integration"]
        catalog_domains = sorted(
            domain
            for domain, metadata in integration_metadata.items()
            if domain in flow_domains and metadata.get("config_flow") is True
        )
        bundled_integrations = {}
        for domain in (
            "habab_home", "habab_telegram", "habab_yandex_speech", "idigital_sms",
        ):
            try:
                integration = await async_get_integration(self.hass, domain)
            except IntegrationNotFound:
                continue
            if integration.config_flow:
                bundled_integrations[domain] = integration
        catalog_domains = sorted(set(catalog_domains) | set(bundled_integrations))
        self.assertIn("met", catalog_domains)
        self.assertNotIn("counter", catalog_domains)
        self.assertEqual(set(bundled_integrations), {
            "habab_home", "habab_telegram", "habab_yandex_speech", "idigital_sms",
        })
        self.assertTrue(integration_metadata["met"]["name"])
        met_translations = await async_get_translations(
            self.hass, self.hass.config.language, "title", integrations=["met"]
        )
        expected_met_name = (
            met_translations.get("component.met.title")
            or integration_metadata["met"]["name"]
        )

        result = await execute(self.hass, None, "owner", {
            "action": "integration_catalog",
            "offset": catalog_domains.index("met"),
            "limit": 1,
        })

        self.assertEqual(result["total"], len(catalog_domains))
        self.assertEqual(result["integrations"], [{
            "domain": "met",
            "name": expected_met_name,
            "integration_type": integration_metadata["met"]["integration_type"],
            "configured": False,
        }])
        self.assertEqual(result["next_offset"], catalog_domains.index("met") + 1)

        bundled_domains = [
            "habab_home", "habab_telegram", "habab_yandex_speech", "idigital_sms",
        ]
        expected_bundle_names = {}
        for domain, integration in bundled_integrations.items():
            translated = await async_get_translations(
                self.hass, self.hass.config.language, "title", integrations=[domain]
            )
            expected_bundle_names[domain] = (
                translated.get(f"component.{domain}.title") or integration.name
            )
        for domain in bundled_domains:
            bundled = await execute(self.hass, None, "owner", {
                "action": "integration_catalog",
                "offset": catalog_domains.index(domain),
                "limit": 1,
            })
            self.assertEqual(len(bundled["integrations"]), 1)
            entry = bundled["integrations"][0]
            self.assertEqual(entry["domain"], domain)
            self.assertTrue(entry["name"])
            self.assertEqual(entry["name"], expected_bundle_names[domain])
            self.assertFalse(entry["configured"])


if __name__ == "__main__":
    unittest.main()
