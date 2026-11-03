"""Voice execution progress must finish on both success and failure."""
import asyncio
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from homeassistant.components.habab_home import realtime


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_working_precedes_slow_executor_ensure_before_runtime_becomes_active(self):
        ensure_entered, release = asyncio.Event(), asyncio.Event()
        runtime = SimpleNamespace(active=None)

        async def ensure():
            ensure_entered.set()
            await release.wait()

        async def execute(*args, **kwargs):
            await ensure()
            return {"session_id": "session", "messages": []}

        assistant = SimpleNamespace(runtime=runtime, run=execute,
            _web_context=lambda owner: {"session_id": "session", "epoch": 3})
        with patch.object(realtime, "async_dispatcher_send") as dispatch:
            task = asyncio.create_task(realtime.run_voice_task(
                None, assistant, "owner", "session", "task", expected_history_epoch=3))
            await ensure_entered.wait()
            self.assertIsNone(runtime.active)
            self.assertEqual(dispatch.call_args.args[1:], (
                "habab_voice_history_owner", {"session_id": "session", "working": True}))
            release.set()
            await task
            self.assertEqual(dispatch.call_args.args[2], {"session_id": "session", "working": False})

    async def test_preparation_failure_and_cancellation_always_clear_immediate_working(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                entered, release = asyncio.Event(), asyncio.Event()

                async def prepare():
                    entered.set()
                    await release.wait()
                    raise ValueError("preparation failed")

                assistant = SimpleNamespace(run=AsyncMock(), runtime=SimpleNamespace(active=None),
                    _web_context=lambda owner: {"session_id": "session", "epoch": 3})
                with patch.object(realtime, "async_dispatcher_send") as dispatch:
                    task = asyncio.create_task(realtime.run_voice_task(
                        None, assistant, "owner", "session", "task", prepare_session=prepare,
                        expected_history_epoch=3))
                    await entered.wait()
                    self.assertEqual(dispatch.call_args.args[2]["working"], True)
                    if cancelled:
                        task.cancel()
                    else:
                        release.set()
                    with self.assertRaises(asyncio.CancelledError if cancelled else ValueError):
                        await task
                    self.assertEqual(dispatch.call_args.args[2], {"session_id": "session", "working": False})
                    assistant.run.assert_not_awaited()

    async def test_retired_epoch_never_publishes_progress_into_replacement_context(self):
        entered, release = asyncio.Event(), asyncio.Event()
        context = {"session_id": "session", "epoch": 3}

        async def execute(*args, **kwargs):
            entered.set()
            await release.wait()
            return {"messages": []}

        assistant = SimpleNamespace(run=execute, runtime=SimpleNamespace(active=None),
                                    _web_context=lambda owner: dict(context))
        with patch.object(realtime, "async_dispatcher_send") as dispatch:
            task = asyncio.create_task(realtime.run_voice_task(
                None, assistant, "owner", "session", "task", expected_history_epoch=3))
            await entered.wait()
            self.assertEqual(dispatch.call_count, 1)
            context.update(epoch=4)
            release.set()
            await task
            self.assertEqual(dispatch.call_count, 1)

    async def test_turn_working_precedes_web_session_ensure_and_uses_validated_canonical_session(self):
        entered, release = asyncio.Event(), asyncio.Event()
        context = {"session_id": "session", "epoch": 3}

        async def web_session(owner):
            entered.set()
            await release.wait()
            return {"session_id": "session"}

        assistant = SimpleNamespace(web_session=AsyncMock(side_effect=web_session),
            voice_history_context=AsyncMock(return_value=dict(context)),
            _web_context=lambda owner: dict(context), runtime=SimpleNamespace(active=None),
            run=AsyncMock(return_value={"session_id": "session", "messages": [{
                "role": "assistant", "text": "Done"}]}))
        owner = SimpleNamespace(id="owner", is_active=True, is_admin=True)

        class Request(dict):
            pass

        request = Request(hass_user=owner)
        request.json = AsyncMock(return_value={"session_id": "voice2_progress", "text": "task"})
        request.app = {"hass": SimpleNamespace(data={"habab_home": {
            "assistant": assistant, "voice_sessions": {"voice2_progress": {
                "owner": "owner", "expires": time.monotonic() + 60,
                "history_session": "session", "history_epoch": 3}}}})}
        with patch.object(realtime, "async_dispatcher_send") as dispatch:
            task = asyncio.create_task(realtime.RealtimeTurnView().post(request))
            await entered.wait()
            self.assertEqual(dispatch.call_args.args[1:], (
                "habab_voice_history_owner", {"session_id": "session", "working": True}))
            assistant.run.assert_not_awaited()
            release.set()
            response = await task
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.body)["text"], "Done")
            events = [call.args[2] for call in dispatch.call_args_list if "working" in call.args[2]]
            self.assertEqual(events[-1], {"session_id": "session", "working": False})
            assistant.run.assert_awaited_once_with("owner", "web", "session", "task",
                                                 expected_history_epoch=3, executor_only=True)

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
