"""Fast model routing preserves ACLs and the canonical channel history."""

import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.components.habab_home.agent_diagnostics import AgentDiagnostics
from homeassistant.components.habab_home.assistant import AssistantService
from homeassistant.components.habab_home.fast_dialog import (
    DIALOGUE_METADATA_KEY, MAX_HISTORY_CHARS, bounded_history, decode_dialogue_part,
    dialogue_part,
)
from homeassistant.components.habab_home.providers import ProviderError
from homeassistant.components.habab_home.setup_agent import AgentError, SetupAgent


def delegate(arguments="{}", name="ask_home_assistant"):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call-1", "type": "function",
        "function": {"name": name, "arguments": arguments},
    }]}


class FastDialogueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        store = SimpleNamespace(async_load=AsyncMock(return_value=None), async_save=AsyncMock())
        self.store_patch = patch("homeassistant.components.habab_home.assistant.Store", return_value=store)
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)
        self.entry = SimpleNamespace(
            data={"provider": "habab", "api_key": "test-key", "model": "test-model"}, options={})
        self.user = SimpleNamespace(is_active=True, is_admin=True)
        self.hass = SimpleNamespace(
            auth=SimpleNamespace(async_get_user=AsyncMock(return_value=self.user)),
            config_entries=SimpleNamespace(async_entries=lambda _domain: [self.entry]),
        )
        self.histories = {"web": [], "tg": [], "tg2": []}
        self.requests = []
        self.agent = object.__new__(SetupAgent)
        self.agent.hass = self.hass
        self.agent.password = "password-fixture"
        self.agent.control_token = "control-fixture"
        self.agent._runtime_secrets = set()
        self.runtime = SimpleNamespace(
            lock=asyncio.Lock(), ensure=AsyncMock(), request=AsyncMock(side_effect=self.request),
            visible=self.agent.visible, run=AsyncMock(side_effect=self.runtime_run),
            diagnostics=AgentDiagnostics(),
        )
        self.service = AssistantService(self.hass, self.runtime)
        self.service.web_sessions = {"alice": "web"}
        self.service.telegram_sessions = {"alice:bot:10": "tg", "alice:bot:11": "tg2"}
        self.provider = AsyncMock(return_value={"role": "assistant", "content": "Привет!"})
        for name, kwargs in (
            ("homeassistant.components.habab_home.fast_dialog.ProviderClient", {"return_value": SimpleNamespace(complete=self.provider)}),
            ("homeassistant.components.habab_home.fast_dialog.build_voice_context", {"side_effect": lambda _hass: {"local_time": "fresh"}}),
            ("homeassistant.components.habab_home.assistant.async_get_clientsession", {"return_value": object()}),
        ):
            mocked = patch(name, **kwargs)
            mocked.start()
            self.addCleanup(mocked.stop)

    async def request(self, method, route, body=None):
        self.requests.append((method, route, copy.deepcopy(body)))
        session = route.split("/")[1]
        history = self.histories[session]
        if method == "GET":
            return copy.deepcopy(history)
        self.assertEqual(body["noReply"], True)
        parts = [{**part, "id": "part-server-id"} for part in body["parts"]]
        history.append({"info": {"id": f"msg-{len(history)}", "role": "user"}, "parts": parts})
        return history[-1]

    async def runtime_run(self, owner, session_id, text):
        self.histories[session_id].extend([
            {"info": {"id": "delegated-user", "role": "user"},
             "parts": [{"type": "text", "text": text}]},
            {"info": {"id": "delegated-answer", "role": "assistant"},
             "parts": [{"type": "text", "text": "Готово"}]},
        ])
        return {"session_id": session_id, "messages": self.runtime.visible(self.histories[session_id])}

    async def test_default_direct_response_persisted_without_executor_model(self):
        self.runtime.diagnostics.record("alice", "web", {"name": "ProviderError"})
        result = await self.service.run("alice", "web", "web", "Привет")
        self.assertEqual(result["messages"], [
            {"role": "user", "text": "Привет"}, {"role": "assistant", "text": "Привет!"}])
        self.assertEqual(result["dialogue"]["route"], "direct")
        self.assertGreaterEqual(result["dialogue"]["total_ms"], 0)
        self.runtime.run.assert_not_awaited()
        self.assertIsNone(self.runtime.diagnostics.get("alice", "web"))
        part = self.histories["web"][0]["parts"][0]
        self.assertIn(DIALOGUE_METADATA_KEY, part["metadata"])
        self.assertFalse(any("voice" in key for key in part["metadata"]))

    async def test_history_continuity_direct_then_delegate_and_original_text(self):
        await self.service.run("alice", "web", "web", "Меня зовут Саша")
        self.provider.return_value = delegate()
        original = "Добавь молоко и напомни моё имя"
        result = await self.service.run("alice", "web", "web", original)
        self.runtime.run.assert_awaited_once_with("alice", "web", original)
        model_messages = self.provider.call_args.args[0]
        self.assertIn({"role": "user", "content": "Меня зовут Саша"}, model_messages)
        self.assertEqual(model_messages[-1], {"role": "user", "content": original})
        self.assertEqual([item["text"] for item in result["messages"]],
                         ["Меня зовут Саша", "Привет!", original, "Готово"])
        self.assertEqual(result["dialogue"]["route"], "opencode")
        self.provider.return_value = {"role": "assistant", "content": "Саша"}
        await self.service.run("alice", "web", "web", "Как меня зовут?")
        self.assertIn({"role": "assistant", "content": "Готово"}, self.provider.call_args.args[0])

    async def test_each_request_has_fresh_context(self):
        with patch("homeassistant.components.habab_home.fast_dialog.build_voice_context",
                   side_effect=[{"local_time": "one"}, {"local_time": "two"}]):
            await self.service.run("alice", "web", "web", "Время?")
            await self.service.run("alice", "web", "web", "А сейчас?")
        snapshots = [json.loads(call.args[0][1]["content"].split("\n", 1)[1])
                     for call in self.provider.call_args_list]
        self.assertEqual(snapshots, [{"local_time": "one"}, {"local_time": "two"}])

    async def test_web_and_two_telegram_histories_stay_isolated(self):
        await self.service.run("alice", "web", "web", "web secret")
        await self.service.run("alice", "telegram", "tg", "tg secret", "bot", "10")
        await self.service.run("alice", "telegram", "tg2", "other chat", "bot", "11")
        for call in self.provider.call_args_list:
            messages = call.args[0]
            self.assertEqual(len(messages), 3)
        self.assertEqual(len(self.histories["web"]), 1)
        with self.assertRaisesRegex(AgentError, "not_found"):
            await self.service.run("alice", "telegram", "web", "cross", "bot", "10")
        self.assertEqual(self.provider.await_count, 3)

    async def test_explicit_disabled_uses_only_old_path(self):
        self.entry.options["fast_dialog"] = False
        result = await self.service.run("alice", "web", "web", "hello")
        self.provider.assert_not_awaited()
        self.runtime.run.assert_awaited_once()
        self.assertEqual(result["dialogue"]["mode"], "opencode")

    async def test_internal_executor_only_bypasses_fast_model_for_voice_tool(self):
        result = await self.service.run(
            "alice", "web", "web", "Проверь свет", executor_only=True)
        self.provider.assert_not_awaited()
        self.runtime.run.assert_awaited_once_with("alice", "web", "Проверь свет")
        self.assertEqual(result["dialogue"]["mode"], "opencode")

    async def test_provider_errors_are_explicit_without_fallback_or_history_append(self):
        for code in ("quota", "timeout", "invalid_auth", "invalid_response"):
            self.provider.side_effect = ProviderError(code)
            with self.subTest(code=code), self.assertRaisesRegex(AgentError, code):
                await self.service.run("alice", "web", "web", "hello")
        self.runtime.run.assert_not_awaited()
        self.assertEqual(self.histories["web"], [])

    async def test_malformed_response_and_tool_never_delegate(self):
        replies = [None, {}, {"role": "assistant", "content": ""},
                   delegate('{"request":"rewritten action"}'), delegate("null"),
                   delegate("{bad"), delegate(name="other_tool"),
                   {**delegate(), "tool_calls": delegate()["tool_calls"] * 2},
                   {**delegate(), "tool_calls": {}},
                   {"role": "assistant", "content": "x" * 12001}]
        for reply in replies:
            self.provider.return_value = reply
            with self.subTest(reply=reply), self.assertRaisesRegex(AgentError, "invalid_response"):
                await self.service.run("alice", "web", "web", "hello")
        self.runtime.run.assert_not_awaited()
        self.assertEqual(self.histories["web"], [])

    async def test_acl_before_call_and_revoked_after_provider(self):
        self.user.is_active = False
        with self.assertRaisesRegex(AgentError, "not_allowed"):
            await self.service.run("alice", "web", "web", "hello")
        self.provider.assert_not_awaited()
        self.user.is_active = True

        async def revoked(*args):
            self.user.is_admin = False
            return delegate()

        self.provider.side_effect = revoked
        with self.assertRaisesRegex(AgentError, "not_allowed"):
            await self.service.run("alice", "web", "web", "hello")
        self.runtime.run.assert_not_awaited()
        self.assertEqual(self.histories["web"], [])

    async def test_session_reset_during_provider_cannot_append_or_delegate(self):
        async def reset(*args):
            self.service.telegram_sessions.clear()
            return {"role": "assistant", "content": "answer"}

        self.provider.side_effect = reset
        with self.assertRaisesRegex(AgentError, "not_found"):
            await self.service.run("alice", "telegram", "tg", "hello", "bot", "10")
        self.runtime.run.assert_not_awaited()
        self.assertEqual(self.histories["tg"], [])

    async def test_provider_timeout_is_bounded_and_does_not_delegate(self):
        async def slow(*args):
            await asyncio.sleep(1)

        self.provider.side_effect = slow
        with patch("homeassistant.components.habab_home.fast_dialog.DIALOGUE_TIMEOUT", 0.001):
            with self.assertRaisesRegex(AgentError, "timeout"):
                await self.service.run("alice", "web", "web", "hello")
        self.runtime.run.assert_not_awaited()
        self.assertEqual(self.histories["web"], [])

    async def test_selected_provider_change_discards_inflight_answer(self):
        async def changed(*args):
            self.entry.data = {**self.entry.data, "provider": "openai"}
            return delegate()

        self.provider.side_effect = changed
        with self.assertRaisesRegex(AgentError, "not_found"):
            await self.service.run("alice", "web", "web", "hello")
        self.runtime.run.assert_not_awaited()

    async def test_user_text_limit_checked_before_model(self):
        for text in ("", " ", "x" * 4001):
            with self.subTest(length=len(text)), self.assertRaisesRegex(AgentError, "invalid_request"):
                await self.service.run("alice", "web", "web", text)
        self.provider.assert_not_awaited()

    async def test_web_abort_cancels_provider_without_history_append(self):
        entered = asyncio.Event()

        async def slow(*args):
            entered.set()
            await asyncio.Event().wait()

        self.provider.side_effect = slow
        running = asyncio.create_task(self.service.run("alice", "web", "web", "hello"))
        await asyncio.wait_for(entered.wait(), 1)
        stopped = await self.service.abort("alice", "web", "web")
        self.assertTrue(stopped)
        with self.assertRaisesRegex(AgentError, "aborted"):
            await running
        self.assertEqual(self.histories["web"], [])
        self.assertEqual(self.service.dialogue_tasks, {})
        self.assertFalse(self.runtime.lock.locked())
        self.runtime.run.assert_not_awaited()

    async def test_telegram_external_cancel_propagates_and_cannot_append(self):
        entered = asyncio.Event()

        async def slow(*args):
            entered.set()
            await asyncio.Event().wait()

        self.provider.side_effect = slow
        running = asyncio.create_task(self.service.run(
            "alice", "telegram", "tg", "hello", "bot", "10"))
        await asyncio.wait_for(entered.wait(), 1)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertEqual(self.histories["tg"], [])
        self.assertEqual(self.service.dialogue_tasks, {})
        self.runtime.run.assert_not_awaited()

    def use_real_executor(self):
        self.runtime.hass = self.hass
        self.runtime._aborting = False
        self.runtime._active_run_completed = False
        self.runtime.settings = lambda: self.entry.data
        self.runtime.active = None
        self.runtime.external_search_status = {}

        async def execute(owner, session_id, text):
            return await SetupAgent.run(self.runtime, owner, session_id, text)

        async def abort(owner, session_id, **kwargs):
            return await SetupAgent.abort(self.runtime, owner, session_id, **kwargs)

        async def abort_and_record(session_id, record_context):
            return await SetupAgent._abort_and_record(self.runtime, session_id, record_context)

        self.runtime._abort_and_record = abort_and_record

        self.runtime.run = AsyncMock(side_effect=execute)
        self.runtime.abort = AsyncMock(side_effect=abort)

    async def test_stop_during_second_executor_ensure_prevents_later_execution(self):
        self.use_real_executor()
        entered = asyncio.Event()

        async def ensure(*args):
            if self.runtime.ensure.await_count == 2:
                entered.set()
                await asyncio.Event().wait()

        self.runtime.ensure.side_effect = ensure
        self.provider.return_value = delegate()
        running = asyncio.create_task(self.service.run("alice", "web", "web", "read state"))
        await asyncio.wait_for(entered.wait(), 1)
        self.assertIsNone(self.runtime.active)
        self.assertTrue(await self.service.abort("alice", "web", "web"))
        with self.assertRaisesRegex(AgentError, "aborted"):
            await running
        self.assertEqual(self.histories["web"], [])
        self.assertFalse(any(method == "POST" for method, _route, _body in self.requests))
        self.assertEqual(self.runtime.ensure.await_count, 2)
        self.assertFalse(self.runtime.lock.locked())
        self.assertFalse(self.service.web_lifecycle_lock.locked())
        self.assertEqual(self.service.dialogue_tasks, {})

    async def test_stop_active_executor_aborts_exact_remote_session_and_unlocks(self):
        self.use_real_executor()
        entered = asyncio.Event()
        remote_aborts = []
        abort_entered = asyncio.Event()
        finish_abort = asyncio.Event()

        async def request(method, route, body=None, **kwargs):
            if method == "POST" and route == "session/web/abort":
                remote_aborts.append(route)
                abort_entered.set()
                await finish_abort.wait()
                return True
            if method == "POST" and route == "session/web/message":
                entered.set()
                await asyncio.Event().wait()
            return await self.request(method, route, body)

        self.runtime.request.side_effect = request
        self.provider.return_value = delegate()
        with patch("homeassistant.components.habab_home.setup_agent.get_assistant_status",
                   AsyncMock(return_value={})):
            running = asyncio.create_task(self.service.run("alice", "web", "web", "read state"))
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual(self.runtime.active, ("alice", "web"))
            with self.assertRaisesRegex(AgentError, "not_found"):
                await self.service.abort("alice", "telegram", "web", "bot", "10")
            self.assertFalse(running.done())
            self.assertTrue(await self.service.abort("alice", "web", "web"))
            await asyncio.wait_for(abort_entered.wait(), 1)
            # Repeated Stop must not cancel the executor's remote abort cleanup.
            self.assertTrue(await self.service.abort("alice", "web", "web"))
            finish_abort.set()
            with self.assertRaisesRegex(AgentError, "aborted"):
                await running
        self.assertEqual(remote_aborts, ["session/web/abort"])
        self.runtime.abort.assert_awaited_once_with("alice", "web", record_context=False)
        self.assertIsNone(self.runtime.active)
        self.assertFalse(self.runtime.lock.locked())
        self.assertFalse(self.service.web_lifecycle_lock.locked())
        self.assertEqual(self.service.dialogue_tasks, {})
        self.assertEqual(self.histories["web"], [])

    async def test_missing_readback_does_not_report_unsaved_answer(self):
        original_request = self.request

        async def missing(method, route, body=None):
            if method == "POST":
                return {}
            return await original_request(method, route, body)

        self.runtime.request.side_effect = missing
        with self.assertRaisesRegex(AgentError, "runtime_error"):
            await self.service.run("alice", "web", "web", "hello")
        self.runtime.run.assert_not_awaited()

    def test_history_bound_and_invalid_generic_part_is_hidden(self):
        history = [{"role": "user", "text": "x" * 8000}] * 100
        bounded = bounded_history(history)
        self.assertLessEqual(len(bounded), 20)
        self.assertLessEqual(sum(len(item["content"]) for item in bounded), MAX_HISTORY_CHARS)
        part = dialogue_part("question", "answer")
        self.assertEqual(decode_dialogue_part(part)[1]["text"], "answer")
        part["text"] = "tampered"
        self.assertIsNone(decode_dialogue_part(part))
        self.assertEqual(self.agent.visible([{"info": {"role": "user"}, "parts": [part]}]), [])


if __name__ == "__main__":
    unittest.main()
