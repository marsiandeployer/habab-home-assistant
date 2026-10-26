"""Voice status reports the selected provider's actual transport contract."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from homeassistant.components.habab_home.assistant_status import get_assistant_status, voice_route_metadata
from homeassistant.components.habab_home import assistant_status
from homeassistant.components.habab_home.const import DOMAIN
from homeassistant.components.habab_home.gemini_live import DEFAULT_MODEL
from homeassistant.components.habab_home.setup_agent import AgentError, PROMPT


class AssistantStatusTests(unittest.IsolatedAsyncioTestCase):
    async def status(self, settings):
        hass = SimpleNamespace(data={})
        return await get_assistant_status(hass, "alice", settings, verify_telegram=False)

    def test_voice_route_readback_separates_configured_provider_from_text_model(self):
        habab = voice_route_metadata({
            "provider": "habab", "api_key": "fixture-key", "model": "gemini-3.1-flash-lite",
        })
        self.assertEqual(habab, {
            "voice_provider": "habab", "voice_protocol": "gemini_live",
            "default_voice_model": DEFAULT_MODEL, "voice_supported": True, "voice_ready": True,
        })
        unsupported = voice_route_metadata({"provider": "deepseek", "api_key": "fixture-key"})
        self.assertEqual(unsupported, {
            "voice_provider": None, "voice_protocol": None, "default_voice_model": None,
            "voice_supported": False, "voice_ready": False,
        })

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
                self.assertIs(
                    voice["supported_clients"]["android_with_native_voice"][
                        "android_local_voice_settings_control_from_chat"
                    ],
                    False,
                )
                self.assertEqual(voice["home_assistant_conversation_entity_exposure"], {
                    "registered": True,
                    "scope": "single_exact_entity",
                    "runtime_status": "not_checked",
                })
                self.assertEqual(result["selected_provider"], provider)
                self.assertEqual(result["selected_text_model"], "fixture-text-model")
                self.assertEqual(voice["observed_client"], {
                    "platform": "not_observed", "version": "not_observed",
                    "microphone": "not_observed", "wake_phrase_match": "not_observed",
                    "outbound_audio": "not_observed",
                })
                self.assertNotIn(secret, json.dumps(result))

    def test_setup_prompt_separates_android_local_settings_from_assist_exposure(self):
        prompt = PROMPT.lower()
        self.assertIn("android_local_voice_settings_control_from_chat=false", prompt)
        self.assertIn("не запрещает управлять экспозицией сущностей ha conversation", prompt)
        self.assertIn("home_assistant_conversation_entity_exposure.registered", prompt)
        self.assertIn("runtime_status=not_checked означает, что настройка ещё не проверялась", prompt)
        self.assertIn("не делай вывод об успешной работе из регистрации возможности", prompt)

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

    async def test_android_web_access_is_in_text_runtime_status(self):
        web_access = {
            "status": "available", "phone_url": "http://127.0.0.1:8123",
            "lan_url": "http://192.168.1.42:8123",
            "lan_urls": ["http://192.168.1.42:8123"],
            "external_reachability": "not_verified",
            "source": "android_connectivity_manager",
        }
        home_snapshot = {"web_access": web_access, "assist_exposure": "unknown", "states": []}
        with patch.object(assistant_status, "build_voice_context", return_value=home_snapshot) as builder:
            result = await self.status({"provider": "habab", "api_key": "fixture-key"})
        self.assertEqual(result["web_access"], web_access)
        self.assertIs(result["web_access"], result["home_snapshot"]["web_access"])
        self.assertEqual(result["home_snapshot"], home_snapshot)
        self.assertEqual(builder.call_count, 1)

    async def test_home_snapshot_is_rebuilt_for_each_status_call(self):
        snapshots = [
            {"generated_at": "2026-10-25T12:00:00+03:00", "expires_at": "2026-10-25T12:00:30+03:00",
             "assist_exposure": "verified", "states": [{"entity_id": "vacuum.home", "state": "cleaning"}],
             "web_access": {"status": "unknown"}},
            {"generated_at": "2026-10-25T12:00:05+03:00", "expires_at": "2026-10-25T12:00:35+03:00",
             "assist_exposure": "verified", "states": [{"entity_id": "vacuum.home", "state": "docked"}],
             "web_access": {"status": "unknown"}},
        ]
        with patch.object(assistant_status, "build_voice_context", side_effect=snapshots) as builder:
            first = await self.status({"provider": "habab", "api_key": "fixture-key"})
            second = await self.status({"provider": "habab", "api_key": "fixture-key"})
        self.assertEqual(first["home_snapshot"]["states"][0]["state"], "cleaning")
        self.assertEqual(second["home_snapshot"]["states"][0]["state"], "docked")
        self.assertNotEqual(first["home_snapshot"]["generated_at"], second["home_snapshot"]["generated_at"])
        self.assertEqual(builder.call_count, 2)

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
