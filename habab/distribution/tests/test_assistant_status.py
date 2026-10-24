"""Voice status reports the selected provider's actual transport contract."""
import json
from types import SimpleNamespace
import unittest

from homeassistant.components.habab_home.assistant_status import get_assistant_status
from homeassistant.components.habab_home.const import DOMAIN
from homeassistant.components.habab_home.gemini_live import DEFAULT_MODEL
from homeassistant.components.habab_home.setup_agent import AgentError


class AssistantStatusTests(unittest.IsolatedAsyncioTestCase):
    async def status(self, settings):
        hass = SimpleNamespace(data={})
        return await get_assistant_status(hass, "alice", settings, verify_telegram=False)

    async def test_voice_transport_matches_each_selected_provider(self):
        expected = {
            "deepseek": (False, None, None, None),
            "openai": (True, "openai_realtime", None, "gpt-realtime-mini"),
            "habab": (True, "gemini_live", "native_v2", DEFAULT_MODEL),
            "gemini": (True, "gemini_live", "native_v2", DEFAULT_MODEL),
        }
        for provider, (supported, protocol, client_kind, model) in expected.items():
            with self.subTest(provider=provider):
                secret = "fixture-private-api-key-" + provider
                result = await self.status({
                    "provider": provider, "api_key": secret, "model": "fixture-text-model",
                })
                voice = result["voice"]
                self.assertEqual(voice["selected_provider_supports_realtime"], supported)
                self.assertEqual(voice["selected_provider_protocol"], protocol)
                self.assertEqual(voice["required_client_kind"], client_kind)
                self.assertEqual(voice["default_voice_model"], model)
                self.assertEqual(result["selected_provider"], provider)
                self.assertEqual(result["selected_text_model"], "fixture-text-model")
                self.assertEqual(voice["observed_client"], {
                    "platform": "not_observed", "version": "not_observed",
                    "microphone": "not_observed", "wake_phrase_match": "not_observed",
                    "outbound_audio": "not_observed",
                })
                self.assertNotIn(secret, json.dumps(result))

    async def test_gemini_live_requires_a_nonempty_string_key_without_claiming_connection(self):
        for provider, api_key in (
            ("habab", None), ("habab", "   "), ("habab", 123),
            ("gemini", None), ("gemini", ""), ("gemini", False),
        ):
            with self.subTest(provider=provider, api_key=api_key):
                result = await self.status({"provider": provider, "api_key": api_key})
                voice = result["voice"]
                self.assertFalse(voice["selected_provider_supports_realtime"])
                self.assertEqual(voice["selected_provider_protocol"], "gemini_live")
                self.assertEqual(voice["required_client_kind"], "native_v2")
                self.assertEqual(voice["default_voice_model"], DEFAULT_MODEL)
                self.assertEqual(voice["observed_client"]["microphone"], "not_observed")

    async def test_openai_and_deepseek_keep_existing_capability_semantics_without_key(self):
        openai = await self.status({"provider": "openai"})
        self.assertTrue(openai["voice"]["selected_provider_supports_realtime"])
        self.assertEqual(openai["voice"]["selected_provider_protocol"], "openai_realtime")
        self.assertEqual(openai["voice"]["default_voice_model"], "gpt-realtime-mini")

        deepseek = await self.status({"provider": "deepseek"})
        self.assertFalse(deepseek["voice"]["selected_provider_supports_realtime"])
        self.assertIsNone(deepseek["voice"]["selected_provider_protocol"])
        self.assertIsNone(deepseek["voice"]["default_voice_model"])

    async def test_absent_or_unknown_provider_reports_no_selected_transport(self):
        hass = SimpleNamespace(data={DOMAIN: {
            "setup_agent": SimpleNamespace(
                settings=lambda: (_ for _ in ()).throw(AgentError("provider_required")))
        }})
        result = await get_assistant_status(hass, "alice", verify_telegram=False)
        self.assertIsNone(result["selected_provider"])
        self.assertFalse(result["voice"]["selected_provider_supports_realtime"])
        self.assertIsNone(result["voice"]["selected_provider_protocol"])
        self.assertIsNone(result["voice"]["required_client_kind"])
        self.assertIsNone(result["voice"]["default_voice_model"])

        unknown = await self.status({"provider": "unknown", "api_key": "fixture-secret"})
        self.assertFalse(unknown["voice"]["selected_provider_supports_realtime"])
        self.assertIsNone(unknown["voice"]["selected_provider_protocol"])
        self.assertNotIn("fixture-secret", json.dumps(unknown))
