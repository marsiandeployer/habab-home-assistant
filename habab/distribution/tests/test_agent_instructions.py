"""Owner instructions persist and become part of actual executor prompts."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from homeassistant.components.habab_home.agent_instructions import (
    MAX_BYTES, read_instructions, write_instructions,
)
from homeassistant.components.habab_home.setup_agent import SetupAgent


class InstructionTests(unittest.IsolatedAsyncioTestCase):
    async def test_next_prompt_reads_saved_owner_instruction(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = object.__new__(SetupAgent)
            agent.root = Path(directory)
            agent.hass = SimpleNamespace(async_add_executor_job=asyncio.to_thread)
            before = await agent.instruction_prompt()
            write_instructions(agent.root / "workspace", "OWNER_TEST_INSTRUCTION_42")
            after = await agent.instruction_prompt()
            self.assertNotIn("OWNER_TEST_INSTRUCTION_42", before)
            self.assertIn("OWNER_TEST_INSTRUCTION_42", after)
            self.assertEqual(read_instructions(agent.root / "workspace"), "OWNER_TEST_INSTRUCTION_42")

    def test_rejected_save_preserves_owner_text(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            write_instructions(workspace, "owner text")
            with self.assertRaises(ValueError):
                write_instructions(workspace, "я" * MAX_BYTES)
            self.assertEqual(read_instructions(workspace), "owner text")
            write_instructions(workspace, "")
            self.assertEqual(read_instructions(workspace), "")

    def test_symlink_does_not_overwrite_other_file(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            other = workspace / "other"
            other.write_text("preserve")
            (workspace / "AGENTS.md").symlink_to(other)
            with self.assertRaises(ValueError):
                write_instructions(workspace, "replace")
            self.assertEqual(other.read_text(), "preserve")

    async def test_telegram_runs_executor_without_fast_dialog(self):
        from unittest.mock import AsyncMock, patch
        from homeassistant.components.habab_home.assistant import AssistantService
        service = object.__new__(AssistantService)
        entry = SimpleNamespace(data={"provider": "deepseek"}, options={"fast_dialog": True})
        service.hass = SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda _: [entry]))
        service.runtime = SimpleNamespace(run=AsyncMock(return_value={"text": "done"}))
        with patch("homeassistant.components.habab_home.assistant.effective_provider_entry", return_value=entry):
            with patch("homeassistant.components.habab_home.assistant.complete_dialogue", new_callable=AsyncMock) as dialogue:
                await service._run_text("owner", "telegram", "session", "Ко измени настройку")
        service.runtime.run.assert_awaited_once_with("owner", "session", "Ко измени настройку")
        dialogue.assert_not_awaited()
