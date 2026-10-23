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
