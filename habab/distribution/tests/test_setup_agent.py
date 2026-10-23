"""Protect owner boundaries, secret filtering and executable integrity checks."""
import asyncio
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from homeassistant.components.habab_home.setup_agent import AgentError, SetupAgent, verify_binary


class AgentTests(unittest.IsolatedAsyncioTestCase):
    def agent(self):
        # Avoid constructing a HA Store: these checks exercise pure ownership
        # and serialization logic, not HA's already-tested storage machinery.
        agent = object.__new__(SetupAgent)
        agent.owners = {"alice": "ses_alice"}
        agent.lock = asyncio.Lock()
        agent.ensure = AsyncMock()
        agent.request = AsyncMock()
        agent.active = ("alice", "ses_alice")
        agent.password = "private-runtime-password"
        agent.control_token = "private-action-token"
        entry = SimpleNamespace(data={"api_key": "private-provider-key"})
        agent.hass = SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda _: [entry]))
        return agent

    async def test_other_administrator_cannot_run_or_abort_session(self):
        agent = self.agent()
        operations = (
            lambda: agent.run("bob", "ses_alice", "test"),
            lambda: agent.abort("bob", "ses_alice"),
        )
        for operation in operations:
            with self.assertRaises(AgentError) as error:
                await operation()
            self.assertEqual(error.exception.code, "not_found")
        agent.ensure.assert_not_awaited()
        agent.request.assert_not_awaited()

    async def test_busy_does_not_start_another_root_command(self):
        agent = self.agent()
        async with agent.lock:
            with self.assertRaises(AgentError) as error:
                await agent.run("alice", "ses_alice", "test")
            self.assertEqual(error.exception.code, "busy")
        agent.ensure.assert_not_awaited()

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
