"""Protect the isolated OpenCode runtime, active capability tuple, and secrets."""
import asyncio
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from homeassistant.components.habab_home.setup_agent import AgentError, SetupAgent, verify_binary


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
