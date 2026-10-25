"""Protect the isolated OpenCode runtime, active capability tuple, and secrets."""
import asyncio
import hashlib
import json
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.habab_home.setup_agent import (
    PROMPT,
    AgentError,
    SetupAgent,
    VOICE_TURN_EVENT_ID_KEY,
    VOICE_TURN_HASH_KEY,
    VOICE_TURN_METADATA_KEY,
    VOICE_TURN_METADATA_VALUE,
    verify_binary,
)
from homeassistant.components.habab_home.voice_history import encode_voice_event
from homeassistant.components.habab_home.assistant_identity import ASSISTANT_IDENTITY
from homeassistant.components.habab_home.runtime_context import (
    ANDROID_PROOT_RUNTIME_KIND,
    runtime_fingerprint,
    runtime_prompt,
)


class AgentTests(unittest.IsolatedAsyncioTestCase):
    def test_provider_selection_supports_openai_but_preserves_existing_deepseek(self):
        agent = object.__new__(SetupAgent)
        openai = SimpleNamespace(data={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "test-openai-key"})
        deepseek = SimpleNamespace(data={"provider": "deepseek", "model": "deepseek-flash", "api_key": "test-deepseek-key"})
        agent.hass = SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda _: [openai]))
        self.assertIs(agent.settings(), openai.data)
        agent.hass.config_entries.async_entries = lambda _: [openai, deepseek]
        self.assertIs(agent.settings(), deepseek.data)

    def test_explicit_selection_precedes_legacy_fallback_and_uses_first_flag(self):
        agent = object.__new__(SetupAgent)
        openai = SimpleNamespace(
            data={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "openai-key"},
            options={"assistant_selected": True},
        )
        deepseek = SimpleNamespace(
            data={"provider": "deepseek", "model": "deepseek-flash", "api_key": "deepseek-key"},
            options={},
        )
        agent.hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_entries=lambda _: [deepseek, openai]))

        self.assertIs(agent.settings(), openai.data)

        deepseek.options = {"assistant_selected": True}
        agent.hass.config_entries.async_entries = lambda _: [openai, deepseek]
        self.assertIs(agent.settings(), openai.data)

    def agent(self):
        agent = object.__new__(SetupAgent)
        agent.lock = asyncio.Lock()
        agent.ensure = AsyncMock()
        agent.request = AsyncMock(side_effect=[
            {"info": {"role": "assistant"}},
            [{"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "OK"}]}],
        ])
        agent.active = None
        agent.password = "private-runtime-password"
        agent.control_token = "private-action-token"
        agent.diagnostics = SimpleNamespace(record=Mock(), clear=Mock())
        entry = SimpleNamespace(data={
            "provider": "deepseek", "api_key": "private-provider-key", "model": "deepseek-flash",
        })
        agent.hass = SimpleNamespace(data={}, config_entries=SimpleNamespace(async_entries=lambda _: [entry]))
        return agent

    async def test_busy_does_not_start_another_root_command(self):
        agent = self.agent()
        async with agent.lock:
            with self.assertRaises(AgentError) as error:
                await agent.run("alice", "ses_alice", "test")
            self.assertEqual(error.exception.code, "busy")
        agent.ensure.assert_not_awaited()

    async def test_run_preserves_active_capability_tuple_and_clears_old_diagnostic(self):
        agent = self.agent()
        telegram = SimpleNamespace(status=AsyncMock(side_effect=AssertionError("unexpected external status lookup")))
        agent.hass.data["habab_telegram"] = telegram
        result = await agent.run("alice", "ses_alice", "test")
        self.assertEqual(result["session_id"], "ses_alice")
        self.assertEqual(agent.active, None)
        self.assertEqual(agent.request.await_args_list[0].args[:2],
                         ("POST", "session/ses_alice/message"))
        self.assertEqual(agent.request.await_args_list[0].args[2]["system"], PROMPT)
        parts = agent.request.await_args_list[0].args[2]["parts"]
        self.assertIs(parts[0]["synthetic"], True)
        status = json.loads(parts[0]["text"].split("\n", 1)[1])
        self.assertEqual(status["selected_provider"], "deepseek")
        self.assertEqual(status["voice"]["observed_client"]["outbound_audio"], "not_observed")
        self.assertEqual(status["telegram"]["phase"], "not_checked")
        telegram.status.assert_not_awaited()
        self.assertNotIn("private-provider-key", parts[0]["text"])
        self.assertEqual(parts[1], {"type": "text", "text": "test"})
        self.assertEqual(agent.request.await_args_list[1].args[:2],
                         ("GET", "session/ses_alice/message?limit=60"))
        agent.diagnostics.clear.assert_called_once_with("alice", "ses_alice")

    async def test_actual_message_uses_selected_provider_instead_of_hardcoded_deepseek(self):
        for provider in ("openai", "habab"):
            agent = self.agent()
            agent.settings = Mock(return_value={"provider": provider, "model": "gpt-4.1-mini", "api_key": "fixture-key"})
            await agent.run("alice", "ses_alice", "test")
            self.assertEqual(agent.request.await_args_list[0].args[2]["model"],
                             {"providerID": "habab-" + provider, "modelID": "gpt-4.1-mini"})

    async def test_selection_changed_during_startup_applies_to_next_turn(self):
        agent = self.agent()
        old = {"provider": "deepseek", "model": "deepseek-flash", "api_key": "fixture-key"}
        agent.settings = Mock(return_value=old)
        async def change_selection():
            agent.settings.return_value = {"provider": "habab", "model": "gpt-4.1-mini", "api_key": "fixture-new-key"}
        agent.ensure = change_selection
        await agent.run("alice", "ses_alice", "test")
        self.assertEqual(agent.request.await_args_list[0].args[2]["model"],
                         {"providerID": "habab-deepseek", "modelID": "deepseek-flash"})

    def test_selection_survives_new_runtime_without_deleting_other_connections(self):
        entries = [SimpleNamespace(data={"provider": p}, options={}) for p in ("deepseek", "habab", "openai")]
        def update(entry, *, options):
            entry.options = options
        agent = object.__new__(SetupAgent)
        agent.hass = SimpleNamespace(config_entries=SimpleNamespace(
            async_entries=lambda _: entries, async_update_entry=update))
        agent.select_provider("habab")
        restarted = object.__new__(SetupAgent)
        restarted.hass = agent.hass
        self.assertIs(restarted.settings(), entries[1].data)
        restarted.select_provider("openai")
        self.assertIs(restarted.settings(), entries[2].data)
        self.assertEqual(sum(bool(e.options["assistant_selected"]) for e in entries), 1)
        self.assertEqual(len(entries), 3)

    async def test_model_error_is_not_hidden_and_safe_diagnostic_is_recorded(self):
        agent = self.agent()
        agent.request.side_effect = [{"info": {"error": {
            "name": "APIError", "message": "provider response includes private-key",
            "data": {"statusCode": 400},
        }} }, None]
        with self.assertRaises(AgentError) as error:
            await agent.run("alice", "ses_alice", "test")
        self.assertEqual(error.exception.code, "model_error")
        agent.diagnostics.record.assert_called_once()

    def test_history_omits_tool_outputs_reasoning_and_synthetic_secrets(self):
        agent = self.agent()
        messages = [{"info": {"role": "assistant"}, "parts": [
            {"type": "reasoning", "text": "hidden"},
            {"type": "tool", "state": {"output": "secret"}},
            {"type": "text", "text": "hidden", "synthetic": True},
            {"type": "text", "text": "private-provider-key private-runtime-password"},
        ]}]
        self.assertEqual(agent.visible(messages), [{"role": "assistant", "text": "[redacted] [redacted]"}])

    def test_visible_projects_individual_voice_events_and_preserves_legacy_pairs(self):
        agent = self.agent()
        user_text = "Установи термостат"
        assistant_text = "Откройте защищённую форму"
        user_raw, _, user_meta = encode_voice_event("gemini-evt-1", "user", user_text)
        assistant_raw, _, assistant_meta = encode_voice_event(
            "gemini-evt-2", "assistant", assistant_text)
        legacy_raw = json.dumps(
            {"user": "Старый вопрос", "assistant": "Старый ответ"},
            ensure_ascii=False, separators=(",", ":"))
        legacy_hash = hashlib.sha256(legacy_raw.encode("utf-8")).hexdigest()
        messages = [
            {"info": {"role": "assistant"}, "parts": [
                {"type": "text", "text": user_raw, "synthetic": True, "metadata": user_meta},
                {"type": "text", "text": assistant_raw, "synthetic": True, "metadata": assistant_meta},
                {"type": "text", "text": legacy_raw, "synthetic": True, "metadata": {
                    VOICE_TURN_METADATA_KEY: VOICE_TURN_METADATA_VALUE,
                    VOICE_TURN_EVENT_ID_KEY: "legacy-event",
                    VOICE_TURN_HASH_KEY: legacy_hash,
                }},
            ]},
        ]
        self.assertEqual(agent.visible(messages), [
            {"role": "user", "text": user_text},
            {"role": "assistant", "text": assistant_text},
            {"role": "user", "text": "Старый вопрос"},
            {"role": "assistant", "text": "Старый ответ"},
        ])

    def test_changed_executable_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "opencode"
            binary.write_bytes(b"original")
            digest = hashlib.sha256(b"original").hexdigest()
            verify_binary(str(binary), digest)
            binary.write_bytes(b"modified")
            with self.assertRaises(AgentError) as error:
                verify_binary(str(binary), digest)
            self.assertEqual(error.exception.code, "binary_changed")


class RuntimeContextTests(unittest.IsolatedAsyncioTestCase):
    def test_shared_identity_preserves_existing_runtime_security_instructions(self):
        self.assertTrue(PROMPT.startswith(ASSISTANT_IDENTITY))
        self.assertEqual(
            hashlib.sha256(PROMPT.removeprefix(ASSISTANT_IDENTITY).encode()).hexdigest(),
            "01e3d7ff9d74deb367fb5dea05e1eb8fbcedac58ae4f3443ff37b963ade0a92d",
        )
        self.assertIs(runtime_prompt("docker", PROMPT), PROMPT)

    def test_android_prompt_uses_proot_sandbox_facts_and_shared_tools(self):
        prompt = runtime_prompt(ANDROID_PROOT_RUNTIME_KIND, PROMPT)
        self.assertTrue(prompt.startswith(ASSISTANT_IDENTITY))
        self.assertIn("UID 0 действует только внутри PRoot-гостя", prompt)
        self.assertIn("это НЕ root Android и НЕ Docker", prompt)
        self.assertIn("не даёт доступа к Bluetooth, Wi-Fi-сканированию, BlueZ", prompt)
        self.assertIn("реальный мост через разрешение/API Android", prompt)
        self.assertIn("Обычные нативные настройки", prompt)
        self.assertIn("Home Assistant и доступные инструменты остаются применимы.", prompt)
        self.assertIn('"action":"start_integration"', prompt)
        self.assertNotIn("ВНУТРИ Docker-контейнера Home Assistant Core", prompt)
        self.assertNotIn("Bluetooth override", prompt)
        self.assertNotIn("Windows Docker Desktop", prompt)
        self.assertNotIn("не предполагает наличие Windows-адаптера", prompt)

    def test_runtime_kind_is_part_of_runtime_fingerprint(self):
        settings = {"provider": "deepseek", "model": "deepseek-flash"}
        self.assertNotEqual(
            runtime_fingerprint(settings, "docker"),
            runtime_fingerprint(settings, ANDROID_PROOT_RUNTIME_KIND),
        )

    async def test_ensure_applies_android_prompt_and_runtime_fingerprint(self):
        settings = {"provider": "deepseek", "model": "deepseek-flash", "api_key": "test-key"}
        process = SimpleNamespace(returncode=None)
        agent = object.__new__(SetupAgent)
        agent.process = None
        agent.fingerprint = None
        agent.root = Path("/private/runtime")
        agent.password = "private-password"
        agent.control_token = "private-control-token"
        agent.hass = SimpleNamespace(async_add_executor_job=AsyncMock())
        agent.settings = Mock(return_value=settings)
        agent.prepare_directories = Mock()
        agent.request = AsyncMock(return_value={})

        with patch.dict("os.environ", {"HABAB_RUNTIME_KIND": ANDROID_PROOT_RUNTIME_KIND}):
            with patch(
                "homeassistant.components.habab_home.setup_agent.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=process,
            ) as spawn:
                await agent.ensure()

        config = json.loads(spawn.await_args.kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(
            config["agent"]["build"]["prompt"],
            runtime_prompt(ANDROID_PROOT_RUNTIME_KIND, PROMPT),
        )
        self.assertEqual(
            agent.fingerprint,
            runtime_fingerprint(settings, ANDROID_PROOT_RUNTIME_KIND),
        )

    async def test_selected_provider_fixed_route_and_private_key_despite_old_relay_env(self):
        for provider, expected_base in (("openai", "https://api.openai.com/v1"), ("habab", "https://habab-voice.wpmix.net/gemini/v1beta")):
            agent = object.__new__(SetupAgent)
            agent.process = None; agent.fingerprint = None; agent.root = Path("/private/runtime")
            agent.password = "private-password"; agent.control_token = "private-control"
            agent.hass = SimpleNamespace(async_add_executor_job=AsyncMock())
            model = "gemini-3.1-flash-lite" if provider == "habab" else "gpt-4.1-mini"
            agent.settings = Mock(return_value={"provider": provider, "model": model, "api_key": "fixture-selected-key"})
            agent.prepare_directories = Mock(); agent.request = AsyncMock(return_value={})
            with patch.dict("os.environ", {
                "HABAB_OPENAI_BASE_URL": "https://voice.example.test/v1",
                "OPENCODE_ENABLE_EXA": "0", "EXA_API_KEY": "fixture-host-exa-key",
            }):
                with patch("homeassistant.components.habab_home.setup_agent.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=SimpleNamespace(returncode=None)) as spawn:
                    await agent.ensure()
            env = spawn.await_args.kwargs["env"]
            config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
            self.assertEqual(config["permission"], {"*": "allow"})
            self.assertEqual(config["enabled_providers"], ["habab-" + provider])
            self.assertEqual(config["model"], "habab-" + provider + "/" + model)
            self.assertEqual(config["provider"]["habab-" + provider]["options"]["baseURL"], expected_base)
            self.assertEqual(config["provider"]["habab-" + provider]["npm"],
                             "@ai-sdk/google" if provider == "habab" else "@ai-sdk/openai-compatible")
            self.assertEqual(env["HABAB_" + provider.upper() + "_KEY"], "fixture-selected-key")
            self.assertEqual(env["OPENCODE_ENABLE_EXA"], "1")
            self.assertNotIn("EXA_API_KEY", env)
            self.assertNotIn("fixture-host-exa-key", str(env))
            self.assertNotIn("HABAB_DEEPSEEK_KEY", env)
            self.assertNotIn("fixture-selected-key", env["OPENCODE_CONFIG_CONTENT"])

    async def test_unknown_runtime_fails_before_settings_or_process_spawn(self):
        agent = object.__new__(SetupAgent)
        agent.process = None
        agent.close = AsyncMock()
        agent.settings = Mock(side_effect=AssertionError("settings read before validation"))

        with patch.dict("os.environ", {"HABAB_RUNTIME_KIND": "android_native"}):
            with patch(
                "homeassistant.components.habab_home.setup_agent.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
            ) as spawn:
                with self.assertRaises(AgentError) as error:
                    await agent.ensure()

        self.assertEqual(error.exception.code, "unsupported_runtime")
        agent.close.assert_awaited_once()
        agent.settings.assert_not_called()
        spawn.assert_not_awaited()
