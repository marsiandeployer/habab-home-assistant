"""The shared chat front door preserves channel histories and checks owner ACLs."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.habab_home.agent_diagnostics import AgentDiagnostics
from homeassistant.components.habab_home.assistant import AssistantService
from homeassistant.components.habab_home.setup_agent import AgentError


class AssistantServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stores = []

        def store_factory(*args, **kwargs):
            store = SimpleNamespace(async_load=AsyncMock(return_value=None), async_save=AsyncMock())
            self.stores.append((args, kwargs, store))
            return store

        self.store_patch = patch("homeassistant.components.habab_home.assistant.Store", side_effect=store_factory)
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)
        self.user = SimpleNamespace(is_active=True, is_admin=True)
        self.hass = SimpleNamespace(auth=SimpleNamespace(async_get_user=AsyncMock(return_value=self.user)))
        self.runtime = SimpleNamespace(
            lock=asyncio.Lock(), ensure=AsyncMock(), request=AsyncMock(), visible=Mock(return_value=[]),
            run=AsyncMock(return_value={"session_id": "s", "messages": []}),
            abort=AsyncMock(return_value=True), active=None, process=None,
            diagnostics=AgentDiagnostics(),
        )
        self.service = AssistantService(self.hass, self.runtime)

    async def test_existing_web_and_telegram_history_maps_are_loaded_unchanged(self):
        self.stores[0][2].async_load.return_value = {"alice": "web-existing"}
        self.stores[1][2].async_load.return_value = {"alice:bot:chat": "tg-existing"}
        await self.service.setup()
        self.runtime.request.return_value = []
        web = await self.service.web_session("alice")
        tg = await self.service.telegram_session("alice", "bot", "chat")
        self.assertEqual(web["session_id"], "web-existing")
        self.assertEqual(tg["session_id"], "tg-existing")
        self.assertEqual(self.service.web_sessions, {"alice": "web-existing"})
        self.assertEqual(self.service.telegram_sessions, {"alice:bot:chat": "tg-existing"})
        for store in self.stores:
            store[2].async_save.assert_not_awaited()

    async def test_web_and_telegram_session_ids_cannot_cross_channels_or_owners(self):
        await self.service.setup()
        self.service.web_sessions = {"alice": "web-session", "bob": "bob-session"}
        self.service.telegram_sessions = {"alice:entry:10": "tg-session"}
        await self.service.run("alice", "web", "web-session", "web text")
        await self.service.run("alice", "telegram", "tg-session", "tg text", "entry", "10")
        self.assertEqual(self.runtime.run.await_count, 2)
        for args in (
            ("bob", "web", "web-session", "x"),
            ("alice", "telegram", "web-session", "x", "entry", "10"),
            ("alice", "web", "tg-session", "x"),
            ("alice", "telegram", "tg-session", "x", "other-entry", "10"),
        ):
            with self.assertRaises(AgentError) as error:
                await self.service.run(*args)
            self.assertEqual(error.exception.code, "not_found")
        self.assertEqual(self.runtime.run.await_count, 2)

    async def test_web_run_rejects_stale_voice_epoch_before_runtime_work(self):
        await self.service.setup()
        self.service.web_sessions = {"alice": "canonical-session"}
        self.service.web_contexts = {"alice": {
            "session_id": "canonical-session", "epoch": 4,
        }}

        with self.assertRaisesRegex(AgentError, "not_found"):
            await self.service.run(
                "alice", "web", "canonical-session", "old lease intent",
                expected_history_epoch=3)

        self.runtime.run.assert_not_awaited()
        self.runtime.ensure.assert_not_awaited()
        self.runtime.request.assert_not_awaited()

    async def test_web_lifecycle_lock_keeps_new_chat_out_until_guarded_run_finishes(self):
        await self.service.setup()
        self.service.web_sessions = {"alice": "canonical-session"}
        self.service.web_contexts = {"alice": {
            "session_id": "canonical-session", "epoch": 0,
        }}
        run_started = asyncio.Event()
        release_run = asyncio.Event()

        async def root_turn(_owner, _session_id, _text):
            run_started.set()
            await release_run.wait()
            return {"session_id": "canonical-session", "messages": []}

        self.runtime.run.side_effect = root_turn
        self.runtime.request.side_effect = [
            {"id": "replacement-session"}, [],
        ]
        guarded_run = asyncio.create_task(self.service.run(
            "alice", "web", "canonical-session", "voice tool intent",
            expected_history_epoch=0))
        await asyncio.wait_for(run_started.wait(), timeout=1)
        new_chat = asyncio.create_task(self.service.web_session("alice", new=True))
        await asyncio.sleep(0)
        self.assertFalse(new_chat.done())
        self.assertEqual(self.service.web_contexts["alice"], {
            "session_id": "canonical-session", "epoch": 0,
        })

        release_run.set()
        await asyncio.wait_for(guarded_run, timeout=1)
        replacement = await asyncio.wait_for(new_chat, timeout=1)
        self.assertEqual(replacement["session_id"], "replacement-session")
        self.assertEqual(self.service.web_contexts["alice"], {
            "session_id": "replacement-session", "epoch": 1,
        })
    async def test_trusted_origin_comes_only_from_active_mapped_session(self):
        await self.service.setup()
        self.hass.data = {"habab_telegram": SimpleNamespace(
            entry_for_owner=Mock(return_value=SimpleNamespace(
                entry_id="bot-entry", data={"bot_id": 123})),
            bindings={"bot-entry": {"bot_id": 123, "chat_id": "42", "user_id": "7"}},
            entries={"bot-entry": {"ready": True, "chat_id": "42", "user_id": "7"}},
        )}
        self.service.web_sessions = {"alice": "web-session"}
        self.service.telegram_sessions = {"alice:bot-entry:42": "tg-session"}
        self.runtime.active = ("alice", "tg-session")
        origin = await self.service.trusted_origin("alice", "tg-session")
        self.assertEqual(origin, {
            "channel": "telegram", "owner": "alice",
            "entry_id": "bot-entry", "chat_id": "42", "bot_id": 123,
        })
        self.assertIsNone(await self.service.trusted_origin("alice", "web-session"))
        self.assertIsNone(await self.service.trusted_origin("bob", "tg-session"))
        self.service.telegram_sessions["alice:other-bot:other-chat"] = "tg-session"
        self.assertIsNone(await self.service.trusted_origin("alice", "tg-session"))
        self.service.telegram_sessions.pop("alice:other-bot:other-chat")
        self.runtime.active = ("alice", "another-session")
        self.assertIsNone(await self.service.trusted_origin("alice", "tg-session"))
        self.user.is_active = False
        self.runtime.active = ("alice", "tg-session")
        with self.assertRaises(AgentError):
            await self.service.trusted_origin("alice", "tg-session")

    async def test_group_member_origin_delegates_live_acl_to_telegram_manager(self):
        await self.service.setup()
        entry = SimpleNamespace(entry_id="bot-entry", data={"bot_id": 123})
        telegram = SimpleNamespace(
            entry_for_owner=Mock(return_value=entry), bindings={}, entries={"bot-entry": {"ready": True}},
            member_origin=AsyncMock(return_value={
                "channel": "telegram", "owner": "alice", "entry_id": "bot-entry",
                "chat_id": "42", "bot_id": 123, "group_id": "-100", "user_id": 42,
            }),
        )
        self.hass.data = {"habab_telegram": telegram}
        self.service.telegram_sessions = {"alice:bot-entry:42": "member-session"}
        self.runtime.active = ("alice", "member-session")

        origin = await self.service.trusted_origin("alice", "member-session")
        self.assertEqual(origin["chat_id"], "42")
        self.assertEqual(origin["group_id"], "-100")
        telegram.member_origin.assert_awaited_once_with(
            "alice", "bot-entry", "42", "member-session")
        self.assertIsNone(await self.service.trusted_origin("alice", "web-session"))

        telegram.member_origin.reset_mock(return_value=True)
        telegram.member_origin.return_value = None
        self.assertIsNone(await self.service.trusted_origin("alice", "member-session"))
        telegram.member_origin.assert_awaited_once()

        self.service.telegram_sessions.pop("alice:bot-entry:42")
        telegram.member_origin.reset_mock()
        self.assertIsNone(await self.service.trusted_origin("alice", "member-session"))
        telegram.member_origin.assert_not_awaited()

    async def test_inactive_or_non_admin_owner_is_rejected_before_runtime(self):
        self.service.web_sessions = {"alice": "web-session"}
        self.user.is_active = False
        with self.assertRaisesRegex(AgentError, "not_allowed"):
            await self.service.run("alice", "web", "web-session", "x")
        self.user.is_active = True
        self.user.is_admin = False
        with self.assertRaisesRegex(AgentError, "not_allowed"):
            await self.service.web_session("alice")
        self.runtime.run.assert_not_awaited()
        self.runtime.ensure.assert_not_awaited()

    async def test_new_web_session_replaces_only_web_mapping(self):
        await self.service.setup()
        self.service.web_sessions = {"alice": "old-web"}
        self.service.telegram_sessions = {"alice:entry:10": "kept-tg"}
        self.runtime.request.side_effect = [
            {"id": "new-web"},
            [{"info": {"role": "user"}, "parts": [{"type": "text", "text": "hello"}]}],
        ]
        result = await self.service.web_session("alice", new=True)
        self.assertEqual(result["session_id"], "new-web")
        self.assertEqual(self.service.telegram_sessions, {"alice:entry:10": "kept-tg"})
        self.stores[0][2].async_save.assert_awaited_once_with({"alice": "new-web"})

    async def test_telegram_forget_revokes_exact_active_session_even_after_owner_disable(self):
        await self.service.setup()
        self.service.telegram_sessions = {
            "alice:entry:10": "active-tg", "alice:other-entry:10": "other-tg",
        }
        self.runtime.active = ("alice", "active-tg")
        self.user.is_active = False
        session_id = await self.service.telegram_forget("alice", "entry", "10")
        self.assertEqual(session_id, "active-tg")
        self.runtime.abort.assert_awaited_once_with("alice", "active-tg")
        self.assertEqual(self.service.telegram_sessions, {"alice:other-entry:10": "other-tg"})
        self.stores[1][2].async_save.assert_awaited_once_with({"alice:other-entry:10": "other-tg"})

    async def test_telegram_reset_preserves_old_mapping_when_runtime_is_busy(self):
        await self.service.setup()
        old = {"alice:entry:10": "old-tg-session"}
        self.service.telegram_sessions = dict(old)
        self.runtime.active = ("alice", "web-session")
        await self.runtime.lock.acquire()
        try:
            with self.assertRaisesRegex(AgentError, "^busy$"):
                await self.service.telegram_reset("alice", "entry", "10")
        finally:
            self.runtime.lock.release()
        self.assertEqual(self.service.telegram_sessions, old)
        self.stores[1][2].async_save.assert_not_awaited()

    async def test_telegram_reset_preserves_old_mapping_when_session_creation_fails(self):
        await self.service.setup()
        old = {"alice:entry:10": "old-tg-session"}
        self.service.telegram_sessions = dict(old)
        self.runtime.active = ("alice", "web-session")
        self.runtime.request.side_effect = AgentError("runtime_error")
        with self.assertRaisesRegex(AgentError, "^runtime_error$"):
            await self.service.telegram_reset("alice", "entry", "10")
        self.assertEqual(self.service.telegram_sessions, old)
        self.stores[1][2].async_save.assert_not_awaited()

    async def test_diagnostics_are_safe_bounded_and_web_session_scoped(self):
        await self.service.setup()
        self.service.web_sessions = {"alice": "web-session"}
        self.service.telegram_sessions = {"alice:entry:10": "tg-session"}
        secret = "api-key-private"
        error = {
            "name": "APIError", "message": f"provider rejected {secret} reasoning_content",
            "data": {"statusCode": 400, "body": f"Bearer {secret}"},
        }
        self.service.diagnostics.record("alice", "web-session", error)
        self.service.diagnostics.record("alice", "tg-session", error)
        result = await self.service.diagnostics_for_web("alice", "web-session")
        encoded = json.dumps(result)
        self.assertNotIn(secret, encoded)
        self.assertNotIn("provider rejected", encoded)
        self.assertEqual(result["error"]["name"], "APIError")
        self.assertEqual(result["error"]["http_status"], 400)
        self.assertTrue(result["error"]["mentions_reasoning_content"])
        with self.assertRaisesRegex(AgentError, "not_found"):
            await self.service.diagnostics_for_web("alice", "tg-session")
        with self.assertRaisesRegex(AgentError, "not_found"):
            await self.service.diagnostics_for_web("bob", "web-session")

    async def test_run_assist_delegates_to_native_adapter(self):
        chat_log = object()
        config_entry = object()
        entry = SimpleNamespace(entry=config_entry, entity_id="conversation.test")
        with patch("homeassistant.components.habab_home.assistant.AssistAdapter") as adapter:
            adapter.return_value.complete = AsyncMock()
            await self.service.run_assist(entry, chat_log)
        adapter.assert_called_once_with(self.hass, config_entry)
        adapter.return_value.complete.assert_awaited_once_with(chat_log, "conversation.test")


if __name__ == "__main__":
    unittest.main()
