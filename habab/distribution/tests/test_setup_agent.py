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
    PROMPT,
    AgentError,
    SetupAgent,
    verify_binary,
)
from homeassistant.components.habab_home.runtime_context import (
    ANDROID_PROOT_RUNTIME_KIND,
    runtime_fingerprint,
    runtime_prompt,
)


class AgentTests(unittest.IsolatedAsyncioTestCase):
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
        agent.hass = SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda _: [entry]))
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
        result = await agent.run("alice", "ses_alice", "test")
        self.assertEqual(result["session_id"], "ses_alice")
        self.assertEqual(agent.active, None)
        self.assertEqual(agent.request.await_args_list[0].args[:2],
                         ("POST", "session/ses_alice/message"))
        self.assertEqual(agent.request.await_args_list[1].args[:2],
                         ("GET", "session/ses_alice/message?limit=60"))
        agent.diagnostics.clear.assert_called_once_with("alice", "ses_alice")

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
    def test_docker_prompt_is_byte_for_byte_unchanged(self):
        self.assertEqual(
            hashlib.sha256(PROMPT.encode()).hexdigest(),
            "2d1bc151f1e28e3053b4ed3ff50fcbc68720a108dcc1736a29a5123081bfa681",
        )
        self.assertIs(runtime_prompt("docker", PROMPT), PROMPT)

    def test_android_prompt_uses_proot_sandbox_facts_and_shared_tools(self):
        prompt = runtime_prompt(ANDROID_PROOT_RUNTIME_KIND, PROMPT)
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
