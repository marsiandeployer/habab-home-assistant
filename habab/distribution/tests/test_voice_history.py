"""Fast native voice journaling and deferred canonical history materialization."""

import asyncio
import copy
import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.components.habab_home.assistant import AssistantService
from homeassistant.components.habab_home.setup_agent import (
    AgentError, SetupAgent, VOICE_TURN_EVENT_ID_KEY, VOICE_TURN_HASH_KEY,
    VOICE_TURN_METADATA_KEY, VOICE_TURN_METADATA_VALUE,
)


class VoiceHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stores = []

        def store_factory(*args, **kwargs):
            store = SimpleNamespace(async_load=AsyncMock(return_value=None), async_save=AsyncMock())
            self.stores.append((args, kwargs, store))
            return store

        self.store_patch = patch("homeassistant.components.habab_home.assistant.Store",
                                 side_effect=store_factory)
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)
        self.user = SimpleNamespace(is_active=True, is_admin=True)
        self.hass = SimpleNamespace(
            auth=SimpleNamespace(async_get_user=AsyncMock(return_value=self.user)),
            config_entries=SimpleNamespace(async_entries=lambda _domain: []),
        )
        self.agent = object.__new__(SetupAgent)
        self.agent.hass = self.hass
        self.agent.password = "runtime-password-fixture"
        self.agent.control_token = "control-token-fixture"
        self.runtime = SimpleNamespace(
            lock=asyncio.Lock(), ensure=AsyncMock(), request=AsyncMock(), run=AsyncMock(),
            abort=AsyncMock(), active=None, process=None, diagnostics=SimpleNamespace(),
            visible=self.agent.visible,
        )
        self.service = AssistantService(self.hass, self.runtime)
        self.history = []
        self.requests = []

        async def request(method, route, body=None, timeout=15):
            self.requests.append((method, route, copy.deepcopy(body)))
            if method == "POST" and route == "session":
                return {"id": f"new-session-{len(self.requests)}"}
            if method == "GET":
                return copy.deepcopy(self.history)
            if method == "POST":
                self.history.append({"info": {"id": f"msg_{len(self.history)}", "role": "user"},
                                     "parts": copy.deepcopy(body["parts"])})
                return {"info": {"id": f"msg_{len(self.history) - 1}"}}
            raise AssertionError(f"unexpected method: {method}")

        self.runtime.request.side_effect = request
        self.event_id = "resp_native_12345"
        self.user_text = "Включи свет"
        self.assistant_text = "Свет включён"

    async def asyncSetUp(self):
        await self.service.setup()
        self.service.web_sessions = {"alice": "web-session"}
        self.service.web_contexts = {"alice": {"session_id": "web-session", "epoch": 0}}

    async def record(self, **overrides):
        values = {"owner": "alice", "session_id": "web-session",
                  "history_epoch": 0, "event_id": self.event_id,
                  "user_text": self.user_text, "assistant_text": self.assistant_text}
        values.update(overrides)
        return await self.service.record_voice_turn(**values)

    async def test_bootstrap_and_writeback_do_not_start_or_contact_runtime(self):
        self.service.web_sessions.clear()
        self.service.web_contexts["alice"] = {"session_id": None, "epoch": 0}
        context = await self.service.voice_history_context("alice")
        self.assertEqual(context, {"session_id": None, "epoch": 0})
        result = await self.record(session_id=None)
        self.assertEqual(result["session_id"], None)
        self.assertEqual(result["messages"], [])
        self.assertTrue(result["recorded"])
        self.runtime.ensure.assert_not_awaited()
        self.runtime.request.assert_not_awaited()
        self.runtime.run.assert_not_awaited()
        saved = self.service.voice_receipt_store.async_save.await_args.args[0]
        receipt = next(iter(saved.values()))
        self.assertEqual(receipt["user_text"], self.user_text)
        self.assertEqual(receipt["assistant_text"], self.assistant_text)

    async def test_pending_pair_projects_and_materializes_on_web_history_read(self):
        result = await self.record()
        self.assertEqual(result["messages"], [
            {"role": "user", "text": self.user_text},
            {"role": "assistant", "text": self.assistant_text},
        ])
        self.runtime.ensure.assert_not_awaited()
        self.runtime.request.assert_not_awaited()
        self.assertTrue(next(iter(self.service.voice_receipts.values()))["user_text"])

        read = await self.service.web_session("alice")
        self.assertEqual(read["messages"], result["messages"])
        self.runtime.ensure.assert_awaited()
        self.assertEqual(self.runtime.ensure.await_count, 2)
        post = next(item for item in self.requests if item[0] == "POST")
        self.assertEqual(post[1], "session/web-session/message")
        self.assertTrue(post[2]["noReply"])
        part = post[2]["parts"][0]
        self.assertTrue(part["synthetic"])
        self.assertEqual(json.loads(part["text"]), {
            "user": self.user_text, "assistant": self.assistant_text,
        })
        self.assertEqual(part["metadata"], {
            VOICE_TURN_METADATA_KEY: VOICE_TURN_METADATA_VALUE,
            VOICE_TURN_EVENT_ID_KEY: self.event_id,
            VOICE_TURN_HASH_KEY: hashlib.sha256(part["text"].encode()).hexdigest(),
        })
        complete = next(iter(self.service.voice_receipts.values()))
        self.assertEqual(complete["state"], "complete")
        self.assertNotIn("user_text", complete)
        self.assertNotIn("assistant_text", complete)

    async def test_idempotence_conflict_and_restart_recovery(self):
        await self.record()
        first = copy.deepcopy(self.service.voice_receipts)
        duplicate = await self.record()
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(len(self.service.voice_receipts), 1)
        with self.assertRaisesRegex(AgentError, "voice_event_conflict"):
            await self.record(assistant_text="изменено")

        # A persisted pending journal can be loaded after HA restart and flushed once.
        self.service.voice_receipts = first
        await self.service.web_session("alice")
        self.assertEqual(len([item for item in self.requests if item[0] == "POST"]), 1)
        self.assertEqual(next(iter(self.service.voice_receipts.values()))["state"], "complete")

    async def test_store_failure_does_not_acknowledge_or_keep_unpersisted_receipt(self):
        self.service.voice_receipt_store.async_save.side_effect = OSError("store failure")
        with self.assertRaises(OSError):
            await self.record()
        self.assertEqual(self.service.voice_receipts, {})
        self.runtime.ensure.assert_not_awaited()
        self.runtime.request.assert_not_awaited()

    async def test_voice_write_is_not_blocked_by_busy_runtime_materialization(self):
        await self.record()
        await self.runtime.lock.acquire()
        materialization = asyncio.create_task(
            self.service._materialize_pending_voice("alice", "web-session", 0))
        await asyncio.sleep(0)
        second = await asyncio.wait_for(self.record(event_id="resp_native_second"), timeout=0.2)
        self.assertTrue(second["recorded"])
        self.assertEqual(len(second["messages"]), 4)
        self.runtime.ensure.assert_not_awaited()
        self.runtime.lock.release()
        await asyncio.wait_for(materialization, timeout=1)

    async def test_owner_session_and_epoch_are_checked_and_new_chat_revokes_old_lease(self):
        with self.assertRaisesRegex(AgentError, "not_found"):
            await self.record(owner="bob")
        with self.assertRaisesRegex(AgentError, "not_found"):
            await self.record(session_id="other-session")
        with self.assertRaisesRegex(AgentError, "not_found"):
            await self.record(history_epoch=1)
        self.assertEqual(self.service.voice_receipts, {})

        await self.record()
        self.runtime.request.reset_mock()
        result = await self.service.web_session("alice", new=True)
        self.assertNotEqual(result["session_id"], "web-session")
        self.assertEqual(self.service.web_contexts["alice"]["epoch"], 1)
        with self.assertRaisesRegex(AgentError, "not_found"):
            await self.record()

    async def test_new_chat_transition_never_exposes_old_session_with_new_epoch(self):
        map_changed = asyncio.Event()
        continue_transition = asyncio.Event()
        original_session = self.service._session

        async def paused_session(*args, **kwargs):
            result = await original_session(*args, **kwargs)
            map_changed.set()
            await continue_transition.wait()
            return result

        self.service._session = paused_session
        transition = asyncio.create_task(self.service.web_session("alice", new=True))
        await asyncio.wait_for(map_changed.wait(), timeout=1)

        context = await self.service.voice_history_context("alice")
        self.assertEqual(context, {"session_id": "web-session", "epoch": 0})
        accepted = await self.record(
            session_id=context["session_id"], history_epoch=context["epoch"],
            event_id="resp_during_new_chat")
        self.assertTrue(accepted["recorded"])

        continue_transition.set()
        new_session = await asyncio.wait_for(transition, timeout=1)
        self.assertEqual(self.service.web_contexts["alice"], {
            "session_id": new_session["session_id"], "epoch": 1,
        })
        receipt = next(iter(self.service.voice_receipts.values()))
        self.assertEqual(receipt["state"], "complete")
        self.assertIn("session/web-session/message",
                      [route for method, route, _body in self.requests if method == "POST"])

    async def test_new_chat_epoch_survives_restart_without_any_voice_receipt(self):
        result = await self.service.web_session("alice", new=True)
        self.service.web_context_store.async_save.assert_awaited()
        persisted_context = copy.deepcopy(
            self.service.web_context_store.async_save.await_args.args[0])
        self.assertEqual(persisted_context["alice"]["epoch"], 1)
        self.assertEqual(self.service.voice_receipts, {})

        restarted = AssistantService(self.hass, self.runtime)
        restarted.web_context_store.async_load.return_value = persisted_context
        restarted.web_store.async_load.return_value = {"alice": result["session_id"]}
        restarted.telegram_store.async_load.return_value = {}
        restarted.voice_receipt_store.async_load.return_value = {}
        await restarted.setup()

        context = await restarted.voice_history_context("alice")
        self.assertEqual(context, {"session_id": result["session_id"], "epoch": 1})
        with self.assertRaisesRegex(AgentError, "not_found"):
            await restarted.record_voice_turn(
                "alice", None, "resp_old_lease", "old question", "old answer",
                history_epoch=0)

    async def test_first_web_session_binds_contextless_pending_pair_without_epoch_bump(self):
        self.service.web_sessions.clear()
        self.service.web_contexts["alice"] = {"session_id": None, "epoch": 0}
        await self.record(session_id=None)
        result = await self.service.web_session("alice")
        self.assertEqual(self.service.web_contexts["alice"]["epoch"], 0)
        self.assertEqual(result["session_id"], self.service.web_sessions["alice"])
        receipt = next(iter(self.service.voice_receipts.values()))
        self.assertEqual(receipt["state"], "complete")

    async def test_cancelled_owner_is_rechecked_after_durable_store_write(self):
        async def save(_value):
            self.user.is_active = False
        self.service.voice_receipt_store.async_save.side_effect = save
        with self.assertRaisesRegex(AgentError, "not_allowed"):
            await self.record()
        self.runtime.ensure.assert_not_awaited()
        self.runtime.request.assert_not_awaited()

    def test_visible_projects_only_server_marked_voice_payloads(self):
        payload = json.dumps({"user": self.user_text, "assistant": self.assistant_text},
                             ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode()).hexdigest()
        visible = self.agent.visible([
            {"info": {"role": "user"}, "parts": [{"type": "text", "text": payload,
             "synthetic": True, "metadata": {
                 VOICE_TURN_METADATA_KEY: VOICE_TURN_METADATA_VALUE,
                 VOICE_TURN_EVENT_ID_KEY: self.event_id, VOICE_TURN_HASH_KEY: digest}}]},
            {"info": {"role": "user"}, "parts": [{"type": "text", "text": payload,
             "synthetic": False}]},
            {"info": {"role": "user"}, "parts": [{"type": "text", "text": "hidden",
             "synthetic": True, "metadata": {VOICE_TURN_METADATA_KEY: "spoof"}}]},
        ])
        self.assertEqual(visible, [
            {"role": "user", "text": self.user_text},
            {"role": "assistant", "text": self.assistant_text},
            {"role": "user", "text": payload},
        ])


if __name__ == "__main__":
    unittest.main()
