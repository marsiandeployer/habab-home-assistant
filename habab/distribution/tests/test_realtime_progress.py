"""Voice execution progress must finish on both success and failure."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from homeassistant.components.habab_home import realtime


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    async def check_execution(self, fail=False):
        sleep = asyncio.sleep
        runtime = SimpleNamespace(active=("owner", "session"),
            request=AsyncMock(return_value=[]), visible=lambda rows: rows)
        assistant = SimpleNamespace(runtime=runtime, voice_journal_lock=asyncio.Lock(),
            _web_context=lambda owner: {"session_id": "session", "epoch": 0},
            _project_pending_voice=lambda *args: [{"role": "user", "text": "voice turn"}])

        async def execute(*args, **kwargs):
            await sleep(0.03)
            if fail:
                raise ValueError("execution failed")
            return {"messages": []}

        assistant.run = execute
        async def tick(delay):
            await sleep(0.001)

        with patch.object(realtime.asyncio, "sleep", side_effect=tick), \
                patch.object(realtime, "async_dispatcher_send") as dispatch:
            if fail:
                with self.assertRaises(ValueError):
                    await realtime.run_voice_task(None, assistant, "owner", "session", "task")
            else:
                await realtime.run_voice_task(None, assistant, "owner", "session", "task")
            events = [call.args[2] for call in dispatch.call_args_list]
            self.assertTrue(any(event.get("working") is True for event in events))
            self.assertTrue(any(event.get("messages") for event in events))
            self.assertEqual(events[-1], {"session_id": "session", "working": False})
            count = dispatch.call_count
            await sleep(0.01)
            self.assertEqual(dispatch.call_count, count)

    async def test_progress_and_cleanup(self):
        await self.check_execution()

    async def test_error_clears_progress(self):
        await self.check_execution(fail=True)
