"""Safety and size bounds for the native voice context snapshot."""

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


MODULE_PATH = Path(__file__).parents[1] / "src" / "habab_home" / "voice_context.py"
SPEC = importlib.util.spec_from_file_location("habab_voice_context_under_test", MODULE_PATH)
voice_context = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(voice_context)


def make_state(entity_id, state="on", *, name=None, attributes=None):
    return SimpleNamespace(
        entity_id=entity_id,
        domain=entity_id.split(".", 1)[0],
        name=name or entity_id,
        state=state,
        attributes=attributes or {},
    )


class Registry:
    def __init__(self, entries=None):
        self.entries = entries or {}

    def async_get(self, key):
        return self.entries.get(key)

    def async_get_area(self, key):
        return self.entries.get(key)


class VoiceContextTests(unittest.TestCase):
    def setUp(self):
        self.exposed = Mock()
        self.entities = Registry()
        self.devices = Registry()
        self.areas = Registry()
        self.hass = SimpleNamespace(
            config=SimpleNamespace(time_zone="Europe/Moscow"),
            states=SimpleNamespace(async_all=lambda: []),
        )
        self.patches = [
            unittest.mock.patch.object(voice_context, "exposed_entities", SimpleNamespace(
                async_should_expose=self.exposed)),
            unittest.mock.patch.object(voice_context, "entity_registry", SimpleNamespace(
                async_get=Mock(return_value=self.entities))),
            unittest.mock.patch.object(voice_context, "device_registry", SimpleNamespace(
                async_get=Mock(return_value=self.devices))),
            unittest.mock.patch.object(voice_context, "area_registry", SimpleNamespace(
                async_get=Mock(return_value=self.areas))),
        ]
        for patcher in self.patches:
            patcher.start()
        self.addCleanup(lambda: [patcher.stop() for patcher in self.patches])

    def test_timestamp_uses_configured_local_zone_and_expires_after_max_age(self):
        now = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
        result = voice_context.build_voice_context(self.hass, now=now)
        self.assertEqual(result["time_zone"], "Europe/Moscow")
        self.assertEqual(result["local_time"], "2026-10-24 15:00:00")
        self.assertEqual(result["generated_at"], "2026-10-24T15:00:00+03:00")
        self.assertEqual(result["expires_at"], "2026-10-24T15:00:30+03:00")
        self.assertEqual(result["max_age_seconds"], 30)
        self.assertEqual(result["snapshot_kind"], "observed_home_state")

    def test_validated_android_web_access_is_carried_in_voice_context(self):
        web_access = {
            "status": "available", "phone_url": "http://127.0.0.1:8123",
            "lan_url": "http://192.168.1.42:8123",
            "lan_urls": ["http://192.168.1.42:8123"],
            "external_reachability": "not_verified",
            "source": "android_connectivity_manager",
        }
        with unittest.mock.patch.object(voice_context, "web_access_context", return_value=web_access):
            result = voice_context.build_voice_context(self.hass)
        self.assertEqual(result["web_access"], web_access)

    def test_only_assist_exposed_visible_allowlisted_weather_and_device_states_are_included(self):
        states = [
            make_state("climate.kitchen", "heat", name="Кухня", attributes={
                "current_temperature": 21.5, "temperature": 22, "api_key": "fixture-secret",
                "latitude": 55.7, "entity_picture": "/private.png"}),
            make_state("light.hidden_registry", "on"),
            make_state("vacuum.hidden_by_user", "docked"),
            make_state("switch.not_exposed", "on"),
            make_state("lock.front_door", "locked"),
            make_state("person.owner", "home"),
            make_state("weather.home", "partlycloudy", name="Погода", attributes={
                "temperature": 7.5, "humidity": 61, "pressure": 1013,
                "wind_speed": 18, "latitude": 55.7, "api_key": "fixture-secret",
                "temperature_unit": "°C", "secret_note": "fixture-secret"}),
        ]
        self.hass.states.async_all = lambda: states
        self.entities.entries = {
            "climate.kitchen": SimpleNamespace(device_id=None, area_id="kitchen"),
            "light.hidden_registry": SimpleNamespace(device_id=None, area_id=None,
                disabled_by="user"),
            "vacuum.hidden_by_user": SimpleNamespace(device_id=None, area_id=None,
                hidden_by="user"),
            "weather.home": SimpleNamespace(device_id=None, area_id=None),
        }
        self.areas.entries = {"kitchen": SimpleNamespace(name="Кухня")}
        self.exposed.side_effect = lambda hass, domain, entity_id: entity_id != "switch.not_exposed"

        result = voice_context.build_voice_context(self.hass)

        self.assertEqual(result["assist_exposure"], "verified")
        self.assertEqual([row["entity_id"] for row in result["states"]], ["climate.kitchen"])
        self.assertEqual(result["states"][0]["room"], "Кухня")
        self.assertEqual(result["states"][0]["current_temperature"], 21.5)
        self.assertEqual(result["weather"]["status"], "available")
        self.assertEqual(result["weather"]["entities"][0]["temperature"], 7.5)
        self.assertEqual(result["weather"]["entities"][0]["wind_speed"], 18)
        serialized = json.dumps(result, ensure_ascii=False)
        for private in ("fixture-secret", "latitude", "entity_picture", "secret_note",
                        "light.hidden_registry", "switch.not_exposed", "lock.front_door",
                        "person.owner", "vacuum.hidden_by_user"):
            self.assertNotIn(private, serialized)
        self.exposed.assert_any_call(self.hass, "conversation", "weather.home")

    def test_missing_weather_is_explicitly_unavailable(self):
        self.exposed.return_value = True
        result = voice_context.build_voice_context(self.hass)
        self.assertEqual(result["weather"], {"status": "unavailable", "entities": []})

    def test_entity_and_byte_limits_are_deterministic_and_mark_truncation(self):
        states = [make_state(f"light.room_{index:02}", "on", name="Комната " + "Кухня" * 30)
                  for index in range(30)]
        states += [make_state(f"weather.home_{index}", "sunny", name="Погода " + "Тепло" * 30)
                   for index in range(4)]
        self.hass.states.async_all = lambda: states
        self.exposed.return_value = True
        result = voice_context.build_voice_context(self.hass)
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(result["states"]), 12)
        self.assertLessEqual(len(result["weather"]["entities"]), 2)
        self.assertLessEqual(len(encoded), 4800)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["states"], sorted(result["states"], key=lambda row: row["entity_id"]))

    def test_missing_or_failing_exposure_helper_fails_closed_with_explicit_unknown(self):
        self.hass.states.async_all = lambda: [make_state("light.private", "on")]
        self.exposed.side_effect = RuntimeError("unsupported exposure API")
        result = voice_context.build_voice_context(self.hass)
        self.assertEqual(result["assist_exposure"], "unknown")
        self.assertEqual(result["states"], [])
        self.assertEqual(result["weather"]["status"], "unknown")
        with unittest.mock.patch.object(voice_context, "exposed_entities", None):
            unsupported = voice_context.build_voice_context(self.hass)
        self.assertEqual(unsupported["assist_exposure"], "unknown")
        self.assertEqual(unsupported["states"], [])


if __name__ == "__main__":
    unittest.main()
