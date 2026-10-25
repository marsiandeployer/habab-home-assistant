"""Protect the isolated OpenCode runtime, active capability tuple, and secrets."""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.habab_home.setup_agent import (
    EXA_CAPABILITY_MAX_BYTES,
    EXA_CAPABILITY_CHUNK_BYTES,
    EXA_CAPABILITY_PROTOCOL,
    EXA_CAPABILITY_RETRY_SECONDS,
    EXA_CAPABILITY_TOOLS,
    EXA_CAPABILITY_TIMEOUT,
    PROMPT,
    AgentError,
    SetupAgent,
    VOICE_TURN_EVENT_ID_KEY,
    VOICE_TURN_HASH_KEY,
    VOICE_TURN_METADATA_KEY,
    VOICE_TURN_METADATA_VALUE,
    exa_mcp_config,
    fetch_habab_exa_capability,
    habab_service_origin,
    verify_binary,
)
from homeassistant.components.habab_home.agent_diagnostics import AgentDiagnostics
from homeassistant.components.habab_home.voice_history import encode_voice_event
from homeassistant.components.habab_home.assistant_identity import ASSISTANT_IDENTITY
from homeassistant.components.habab_home.runtime_context import (
    ANDROID_PROOT_RUNTIME_KIND,
    runtime_fingerprint,
    runtime_prompt,
)


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_exa_capability_requires_authenticated_fixed_contract_and_is_bounded(self):
        class FragmentedContent:
            def __init__(self, payload, first_fragment=False):
                self.fragments = [payload[:1], payload[1:]] if first_fragment else [payload]
                self.read_sizes = []

            async def read(self, size):
                self.read_sizes.append(size)
                if not self.fragments:
                    return b""
                if len(self.fragments) == 1 and self.read_sizes[-1] > 0:
                    await asyncio.sleep(0.03)
                fragment = self.fragments.pop(0)
                result, rest = fragment[:size], fragment[size:]
                if rest:
                    self.fragments.insert(0, rest)
                return result

        class Response:
            status = 200

            def __init__(self, payload, headers=None, first_fragment=False):
                self.headers = headers or {}
                self.content = FragmentedContent(payload, first_fragment=first_fragment)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

        payload = json.dumps({
            "protocol": EXA_CAPABILITY_PROTOCOL,
            "mcp_url": "/mcp/exa",
            "tools": sorted(EXA_CAPABILITY_TOOLS),
        }).encode()
        response = Response(payload, first_fragment=True)
        client = SimpleNamespace(get=Mock(return_value=response))

        self.assertTrue(await fetch_habab_exa_capability(client, "fixture-service-key"))
        args, kwargs = client.get.call_args
        self.assertEqual(args, ("https://habab-voice.wpmix.net/mcp/exa/capabilities",))
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer fixture-service-key"})
        self.assertEqual(kwargs["timeout"].total, EXA_CAPABILITY_TIMEOUT)
        self.assertFalse(kwargs["allow_redirects"])
        self.assertGreaterEqual(len(response.content.read_sizes), 3)
        self.assertLessEqual(max(response.content.read_sizes), EXA_CAPABILITY_CHUNK_BYTES)

        self.assertEqual(habab_service_origin(), "https://habab-voice.wpmix.net")
        config = exa_mcp_config()
        self.assertEqual(config["url"], "https://habab-voice.wpmix.net/mcp/exa")
        self.assertEqual(config["headers"], {"Authorization": "Bearer {env:HABAB_HABAB_KEY}"})
        self.assertFalse(config["oauth"])
        self.assertNotIn("fixture-service-key", json.dumps(config))

    async def test_exa_capability_rejects_wrong_contract_redirect_status_and_oversize(self):
        class BoundedContent:
            def __init__(self, payload):
                self.remaining = bytearray(payload)
                self.read_sizes = []
                self.total_read = 0

            async def read(self, size):
                self.read_sizes.append(size)
                data = bytes(self.remaining[:size])
                del self.remaining[:size]
                self.total_read += len(data)
                return data

        class Response:
            def __init__(self, status, payload=b"{}", headers=None):
                self.status = status
                self.headers = headers or {}
                self.content = BoundedContent(payload)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

        good = {"protocol": EXA_CAPABILITY_PROTOCOL, "mcp_url": "/mcp/exa",
                "tools": sorted(EXA_CAPABILITY_TOOLS)}
        bad_payloads = [
            {**good, "protocol": "other"},
            {**good, "mcp_url": "https://attacker.invalid/mcp/exa"},
            {**good, "tools": ["web_search_exa"]},
            {**good, "tools": [*good["tools"], "extra_tool"]},
        ]
        for status, payload, headers, expected in [
            (200, json.dumps(item).encode(), {}, False) for item in bad_payloads
        ] + [
            (401, json.dumps(good).encode(), {}, False),
            (302, json.dumps(good).encode(), {}, False),
            (200, b"{}", {"Content-Length": str(EXA_CAPABILITY_MAX_BYTES + 1)}, False),
            (200, b"x" * (EXA_CAPABILITY_MAX_BYTES + 1), {}, False),
        ]:
            response = Response(status, payload, headers)
            client = SimpleNamespace(get=Mock(return_value=response))
            self.assertEqual(await fetch_habab_exa_capability(client, "fixture-key"), expected)
            self.assertLessEqual(response.content.total_read, EXA_CAPABILITY_MAX_BYTES + 1)

    def test_exa_origin_rejects_noncanonical_provider_route(self):
        with patch("homeassistant.components.habab_home.setup_agent.provider_api_base",
                   return_value="https://attacker.invalid/mcp/exa"):
            with self.assertRaises(ValueError):
                habab_service_origin()

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
        agent.external_search_status = {"mode": "not_checked", "state": "not_checked"}
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
        agent.external_search_status = {
            "mode": "habab_exa_mcp_relay", "state": "unavailable",
            "reason": "capability_check_failed",
        }
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
        self.assertEqual(status["external_search"], agent.external_search_status)
        self.assertEqual(status["voice"]["observed_client"]["outbound_audio"], "not_observed")
        self.assertEqual(status["telegram"]["phase"], "not_checked")
        telegram.status.assert_not_awaited()
        self.assertNotIn("private-provider-key", parts[0]["text"])
        self.assertEqual(parts[1], {"type": "text", "text": "test"})
        self.assertEqual(agent.request.await_args_list[1].args[:2],
                         ("GET", "session/ses_alice/message?limit=60"))
        agent.diagnostics.clear.assert_called_once_with("alice", "ses_alice")

    async def test_each_turn_injects_fresh_snapshot_alongside_prior_history(self):
        agent = self.agent()
        prior = {"info": {"role": "assistant"}, "parts": [
            {"type": "text", "text": "После прошлого запуска пылесос был готов к уборке."}
        ]}
        agent.request = AsyncMock(side_effect=[
            {"info": {"role": "assistant"}}, [prior],
            {"info": {"role": "assistant"}}, [prior],
        ])
        status = {
            "selected_provider": "deepseek", "voice": {"observed_client": {"outbound_audio": "not_observed"}},
            "telegram": {"phase": "not_checked"},
        }
        first_snapshot = {
            "generated_at": "2026-10-25T12:00:00+03:00", "expires_at": "2026-10-25T12:00:30+03:00",
            "assist_exposure": "verified", "states": [{"entity_id": "vacuum.home", "state": "cleaning"}],
        }
        second_snapshot = {
            "generated_at": "2026-10-25T12:00:05+03:00", "expires_at": "2026-10-25T12:00:35+03:00",
            "assist_exposure": "verified", "states": [{"entity_id": "vacuum.home", "state": "docked"}],
        }
        with patch("homeassistant.components.habab_home.setup_agent.get_assistant_status",
                   new=AsyncMock(side_effect=[{**status, "home_snapshot": first_snapshot},
                                              {**status, "home_snapshot": second_snapshot}])):
            first_result = await agent.run("alice", "ses_alice", "проверь состояние")
            second_result = await agent.run("alice", "ses_alice", "проверь ещё раз")

        payloads = [call.args[2] for call in agent.request.await_args_list if call.args[0] == "POST"]
        injected = [json.loads(payload["parts"][0]["text"].split("\n", 1)[1]) for payload in payloads]
        self.assertEqual(injected[0]["home_snapshot"]["states"][0]["state"], "cleaning")
        self.assertEqual(injected[1]["home_snapshot"]["states"][0]["state"], "docked")
        self.assertNotEqual(injected[0]["home_snapshot"]["generated_at"],
                            injected[1]["home_snapshot"]["generated_at"])
        self.assertEqual(first_result["messages"][0]["text"], prior["parts"][0]["text"])
        self.assertEqual(second_result["messages"][0]["text"], prior["parts"][0]["text"])

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

    async def test_runtime_request_timeout_keeps_safe_code_and_original_cause(self):
        agent = self.agent()
        del agent.request

        class TimedOutRequest:
            async def __aenter__(self):
                raise TimeoutError("private provider detail")

            async def __aexit__(self, *_):
                return None

        session = SimpleNamespace(request=Mock(return_value=TimedOutRequest()))
        with patch("homeassistant.components.habab_home.setup_agent.async_get_clientsession",
                   return_value=session):
            with self.assertRaises(AgentError) as error:
                await agent.request("POST", "session/test/message", timeout=180)

        self.assertEqual(error.exception.code, "runtime_timeout")
        self.assertIsInstance(error.exception.__cause__, TimeoutError)
        self.assertNotIn("private provider detail", str(error.exception))

    async def test_run_records_timeout_diagnostic_aborts_and_clears_active(self):
        agent = self.agent()
        agent.diagnostics = AgentDiagnostics()
        agent.request.side_effect = AgentError("runtime_timeout")
        agent.abort = AsyncMock(return_value=True)

        with self.assertRaises(AgentError) as error:
            await agent.run("alice", "ses_alice", "test")

        self.assertEqual(error.exception.code, "runtime_timeout")
        diagnostic = agent.diagnostics.get("alice", "ses_alice")
        self.assertTrue(diagnostic["mentions_timeout"])
        self.assertNotIn("message", diagnostic)
        agent.abort.assert_awaited_once_with("alice", "ses_alice")
        self.assertIsNone(agent.active)

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
            "e9c640f608423af1590301aad210a24e08ec3353ad1af386576bc872b41d29b7",
        )
        self.assertIs(runtime_prompt("docker", PROMPT), PROMPT)

    def test_feedback_action_uses_only_the_current_mapped_chat(self):
        instruction = next(
            line
            for line in PROMPT.splitlines()
            if line.startswith("Сохраняй отзыв через save_feedback")
        )
        self.assertIn("текущего server-mapped веб-чата или Telegram-чата", instruction)
        self.assertIn(
            "owner, session_id и channel не передавай", instruction
        )
        self.assertIn(
            "state=saved_locally и issue_state=not_configured", instruction
        )
        self.assertIn(
            "не утверждай, что отзыв отправлен разработчикам или что issue создан",
            instruction,
        )

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
        self.assertEqual(json.loads(spawn.await_args.kwargs["env"]["OPENCODE_CONFIG_CONTENT"])["mcp"], {})

    async def test_selected_provider_search_route_and_private_key_despite_old_relay_env(self):
        providers = (
            ("openai", "gpt-4.1-mini", "https://api.openai.com/v1", "@ai-sdk/openai-compatible"),
            ("deepseek", "deepseek-flash", "https://api.deepseek.com", "@ai-sdk/openai-compatible"),
            ("gemini", "gemini-3.1-flash-lite", "https://generativelanguage.googleapis.com/v1beta", "@ai-sdk/google"),
            ("habab", "gemini-3.1-flash-lite", "https://habab-voice.wpmix.net/gemini/v1beta", "@ai-sdk/google"),
        )
        for provider, model, expected_base, expected_npm in providers:
            agent = object.__new__(SetupAgent)
            agent.process = None; agent.fingerprint = None; agent.root = Path("/private/runtime")
            agent.password = "private-password"; agent.control_token = "private-control"
            agent.hass = SimpleNamespace(async_add_executor_job=AsyncMock())
            agent.settings = Mock(return_value={"provider": provider, "model": model, "api_key": "fixture-selected-key"})
            agent.prepare_directories = Mock(); agent.request = AsyncMock(return_value={})
            with patch.dict("os.environ", {
                "HABAB_OPENAI_BASE_URL": "https://voice.example.test/v1",
                "OPENCODE_ENABLE_EXA": "0", "EXA_API_KEY": "fixture-host-exa-key",
            }):
                with patch("homeassistant.components.habab_home.setup_agent.fetch_habab_exa_capability",
                           new=AsyncMock(return_value=True)) as capability:
                    with patch("homeassistant.components.habab_home.setup_agent.async_get_clientsession",
                               return_value=object()):
                        with patch("homeassistant.components.habab_home.setup_agent.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=SimpleNamespace(returncode=None)) as spawn:
                            await agent.ensure()
                            if provider == "habab":
                                await agent.ensure()
                    if provider == "habab":
                        capability.assert_awaited_once()
                    else:
                        capability.assert_not_awaited()
            env = spawn.await_args.kwargs["env"]
            config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
            self.assertEqual(config["permission"], {"*": "allow"})
            self.assertEqual(config["enabled_providers"], ["habab-" + provider])
            self.assertEqual(config["model"], "habab-" + provider + "/" + model)
            self.assertEqual(config["provider"]["habab-" + provider]["options"]["baseURL"], expected_base)
            self.assertEqual(config["provider"]["habab-" + provider]["npm"], expected_npm)
            self.assertEqual(env["HABAB_" + provider.upper() + "_KEY"], "fixture-selected-key")
            self.assertEqual(env["OPENCODE_ENABLE_EXA"], "0" if provider == "habab" else "1")
            self.assertEqual(config["mcp"], {"exa": exa_mcp_config()} if provider == "habab" else {})
            self.assertNotIn("EXA_API_KEY", env)
            self.assertNotIn("fixture-host-exa-key", str(env))
            if provider != "deepseek":
                self.assertNotIn("HABAB_DEEPSEEK_KEY", env)
            self.assertNotIn("fixture-selected-key", env["OPENCODE_CONFIG_CONTENT"])

    async def test_habab_exa_outage_leaves_normal_runtime_enabled_but_marks_search_unavailable(self):
        agent = object.__new__(SetupAgent)
        agent.process = None; agent.fingerprint = None; agent.root = Path("/private/runtime")
        agent.password = "private-password"; agent.control_token = "private-control"
        agent.hass = SimpleNamespace(data={}, async_add_executor_job=AsyncMock())
        agent.settings = Mock(return_value={
            "provider": "habab", "model": "gemini-3.1-flash-lite", "api_key": "fixture-key"})
        agent.prepare_directories = Mock(); agent.request = AsyncMock(return_value={})
        with patch("homeassistant.components.habab_home.setup_agent.fetch_habab_exa_capability",
                   new=AsyncMock(return_value=False)):
            with patch("homeassistant.components.habab_home.setup_agent.async_get_clientsession",
                       return_value=object()):
                with patch("homeassistant.components.habab_home.setup_agent.asyncio.create_subprocess_exec",
                           new_callable=AsyncMock, return_value=SimpleNamespace(returncode=None)) as spawn:
                    await agent.ensure()

        env = spawn.await_args.kwargs["env"]
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(env["OPENCODE_ENABLE_EXA"], "0")
        self.assertEqual(config["mcp"], {})
        self.assertEqual(agent.external_search_status, {
            "mode": "habab_exa_mcp_relay", "state": "unavailable",
            "reason": "capability_check_failed"})

    async def test_habab_exa_retry_is_bounded_and_restarts_only_after_verified_recovery(self):
        settings = {"provider": "habab", "model": "gemini-3.1-flash-lite", "api_key": "fixture-key"}
        agent = object.__new__(SetupAgent)
        agent.process = SimpleNamespace(returncode=None)
        agent.fingerprint = runtime_fingerprint(settings, "docker")
        agent.external_search_status = {
            "mode": "habab_exa_mcp_relay", "state": "unavailable",
            "reason": "capability_check_failed"}
        agent.external_search_retry_at = 200.0
        agent.root = Path("/private/runtime")
        agent.password = "private-password"; agent.control_token = "private-control"
        agent.hass = SimpleNamespace(async_add_executor_job=AsyncMock())
        agent.settings = Mock(return_value=settings)
        agent.prepare_directories = Mock()
        agent.close = AsyncMock()
        agent.request = AsyncMock(return_value={})

        with patch("homeassistant.components.habab_home.setup_agent.time.monotonic", return_value=199.0):
            with patch("homeassistant.components.habab_home.setup_agent.fetch_habab_exa_capability",
                       new=AsyncMock(return_value=True)) as capability:
                with patch("homeassistant.components.habab_home.setup_agent.async_get_clientsession") as session:
                    await agent.ensure()
                capability.assert_not_awaited()
                session.assert_not_called()
        agent.close.assert_not_awaited()

        with patch("homeassistant.components.habab_home.setup_agent.time.monotonic",
                   side_effect=[200.0, 200.0]):
            with patch("homeassistant.components.habab_home.setup_agent.fetch_habab_exa_capability",
                       new=AsyncMock(return_value=False)) as capability:
                with patch("homeassistant.components.habab_home.setup_agent.async_get_clientsession",
                           return_value=object()):
                    await agent.ensure()
                capability.assert_awaited_once()
        self.assertEqual(agent.external_search_retry_at, 200.0 + EXA_CAPABILITY_RETRY_SECONDS)
        agent.close.assert_not_awaited()

        with patch("homeassistant.components.habab_home.setup_agent.time.monotonic",
                   side_effect=[200.0 + EXA_CAPABILITY_RETRY_SECONDS] * 2):
            with patch("homeassistant.components.habab_home.setup_agent.fetch_habab_exa_capability",
                       new=AsyncMock(return_value=True)) as capability:
                with patch("homeassistant.components.habab_home.setup_agent.async_get_clientsession",
                           return_value=object()):
                    with patch("homeassistant.components.habab_home.setup_agent.asyncio.create_subprocess_exec",
                               new_callable=AsyncMock,
                               return_value=SimpleNamespace(returncode=None)) as spawn:
                        await agent.ensure()
                capability.assert_awaited_once()

        agent.close.assert_awaited_once()
        self.assertEqual(agent.external_search_status["state"], "capability_verified")
        self.assertIsNone(agent.external_search_retry_at)
        config = json.loads(spawn.await_args.kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["mcp"], {"exa": exa_mcp_config()})

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
