"""Security and session boundaries for the native Telegram bridge."""
import asyncio
import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.config_entries import ConfigEntryChange
from homeassistant.components.habab_telegram import TelegramManager
from homeassistant.components.habab_telegram.inbox import TelegramInbox
from homeassistant.components.habab_telegram.config_flow import get_bot
from homeassistant.components.habab_telegram.const import (
    BOT_ONBOARDING_MESSAGE, OWNER_PAIRING_COMPLETE_MESSAGE, UNKNOWN_MEMBER_MESSAGE,
)
from homeassistant.components.habab_home.effective_provider import effective_provider_entry


class SessionBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def test_web_and_telegram_sessions_are_channel_scoped(self):
        from homeassistant.components.habab_home.assistant import AssistantService

        service = object.__new__(AssistantService)
        service.web_sessions = {"alice": "web-session"}
        service.telegram_sessions = {"alice:bot-a:chat-1": "tg-session"}
        self.assertTrue(service._mapped_session("alice", "web", "web-session"))
        self.assertTrue(service._mapped_session("alice", "telegram", "tg-session", "bot-a", "chat-1"))
        self.assertFalse(service._mapped_session("bob", "web", "web-session"))
        self.assertFalse(service._mapped_session("alice", "web", "tg-session"))
        self.assertFalse(service._mapped_session("alice", "telegram", "tg-session", "other", "chat-1"))


class TelegramManagerTests(unittest.IsolatedAsyncioTestCase):
    def manager(self):
        manager = object.__new__(TelegramManager)
        manager.hass = SimpleNamespace(
            auth=SimpleNamespace(async_get_user=AsyncMock(return_value=SimpleNamespace(is_active=True, is_admin=True))),
            data={"habab_home": {
                "setup_agent": SimpleNamespace(active=None),
                "assistant": SimpleNamespace(
                    telegram_sessions={}, telegram_forget=AsyncMock(), abort=AsyncMock(),
                    telegram_reset=AsyncMock(), telegram_session=AsyncMock(), run=AsyncMock(),
                ),
            }},
            async_create_task=asyncio.create_task,
        )
        manager.maintenance = False
        manager.hass.config = SimpleNamespace(path=lambda name: "/fixture-config/" + name)
        manager.entries = {"entry": {"chat_id": "10", "user_id": "20", "username": "testbot"}}
        manager.entries["entry"]["ready"] = True
        manager.group_bindings = {}
        manager.group_pairing = {}
        manager.group_candidates = {}
        manager.group_candidate_tasks = {}
        manager.group_bindings_store = SimpleNamespace(async_save=AsyncMock())
        manager.group_member_chats = {}
        manager.pending_members = {}
        manager.get_chat_member = AsyncMock()
        manager.pairing = {}
        manager.pair_locks = {}
        manager.bindings = {}
        manager.bindings_store = SimpleNamespace(async_save=AsyncMock())
        manager.dedupe = set()
        manager.dedupe_order = []
        manager.store = SimpleNamespace(async_save=AsyncMock())
        manager.send = AsyncMock()
        manager._write = AsyncMock(return_value=True)
        manager.queues = {}
        manager.tasks = {}
        manager.turn_tasks = {}
        manager.control_tasks = {}
        manager.control_lanes = {}
        manager.pending_message_ids = {}
        manager.cancelled_through = {}
        manager.cancel_epochs = {}
        manager.pending_group_cancels = {}
        manager.pending_sends = {}
        manager.inbox = TelegramInbox(SimpleNamespace(async_load=AsyncMock(return_value={}), async_save=AsyncMock()))
        manager.inbox_dispatched = set()
        manager.waiting_updates = {}
        manager.provider_config_epoch = 0
        manager.provider_entry_unsubscribers = {}
        manager.unavailable_provider_entries = set()
        manager.selected_provider_entry_id = None
        manager.provider_refresh_task = None
        manager.transport_errors = {}
        manager._unsubscribe_provider_changes = lambda: None
        manager._closed = False
        return manager

    async def test_entry_polling_is_backgrounded_and_explicitly_stopped(self):
        manager = self.manager()
        normal_tasks = []
        background_tasks = []

        async def async_block_till_done():
            # HA startup waits for ordinary tasks, while background tasks are
            # deliberately excluded from this set.
            if normal_tasks:
                await asyncio.gather(*normal_tasks)

        def create_task(coro, name=None):
            task = asyncio.create_task(coro, name=name)
            normal_tasks.append(task)
            return task

        def create_background_task(coro, name, eager_start=True):
            task = asyncio.create_task(coro, name=name)
            background_tasks.append(task)
            return task

        manager.hass.async_create_task = create_task
        manager.hass.async_create_background_task = create_background_task
        manager.hass.async_block_till_done = async_block_till_done
        manager.consumer_tasks = {}
        manager.hass.data["habab_home"]["setup_agent"].settings = Mock(return_value={
            "provider": "habab", "api_key": "fixture-service-key",
        })

        blocked = asyncio.Event()

        class Stdout:
            async def readline(self):
                await blocked.wait()
                return b""

        process = SimpleNamespace(
            stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()),
            stdout=Stdout(),
            wait=AsyncMock(return_value=0),
            pid=123,
        )
        entry = SimpleNamespace(entry_id="entry", data={
            "token": "fixture-token", "bot_id": 99, "bot_username": "testbot",
            "owner_id": "alice",
        })

        with patch(
            "homeassistant.components.habab_telegram.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ) as create_process, patch(
            "homeassistant.components.habab_telegram.async_get_clientsession",
            return_value=object(),
        ), patch(
            "homeassistant.components.habab_telegram.resolve_telegram_transport",
            new=AsyncMock(return_value=({
                "mode": "habab_relay", "base_url": "https://habab-voice.wpmix.net",
                "service_key": "fixture-service-key",
                "protocol": "telegram-bot-api-relay-v1",
            }, "habab")),
        ), patch.dict("os.environ", {"HABAB_TELEGRAM_RELAY": "https://legacy.test"}):
            await manager.async_setup_entry(entry)

        await asyncio.wait_for(manager.hass.async_block_till_done(), timeout=0.1)
        self.assertEqual(normal_tasks, [])
        self.assertEqual(len(background_tasks), 2)
        self.assertEqual(manager.tasks["entry"], background_tasks[0])
        self.assertEqual(manager.consumer_tasks["entry"], background_tasks[1])
        self.assertIn("reader", background_tasks[0].get_name())
        self.assertIn("consumer", background_tasks[1].get_name())

        await asyncio.sleep(0)
        self.assertFalse(any(task.done() for task in background_tasks))
        await manager.close_entry("entry")
        self.assertTrue(all(task.done() for task in background_tasks))
        self.assertTrue(all(task.cancelled() for task in background_tasks))
        self.assertEqual(process.stdin.write.call_count, 2)  # configure, then shutdown
        self.assertEqual(process.stdin.write.call_args.args[0], b'{"type":"shutdown"}\n')
        configure = json.loads(process.stdin.write.call_args_list[0].args[0])
        self.assertEqual(configure["token"], "fixture-token")
        self.assertEqual(configure["telegram_transport"], {
            "mode": "habab_relay", "base_url": "https://habab-voice.wpmix.net",
            "service_key": "fixture-service-key",
            "protocol": "telegram-bot-api-relay-v1",
        })
        self.assertNotIn("token", create_process.call_args.kwargs["env"])
        self.assertNotIn("fixture-service-key", str(create_process.call_args.kwargs["env"]))
        self.assertNotIn("HABAB_TELEGRAM_RELAY", create_process.call_args.kwargs["env"])
        process.wait.assert_awaited_once_with()

    async def test_provider_change_reloads_worker_route(self):
        manager = self.manager()
        manager.entries["entry"]["transport_identity"] = ("direct",)
        manager.hass.data["habab_home"]["setup_agent"].settings = Mock(return_value={
            "provider": "habab", "api_key": "fixture-service-key",
        })
        config_entry = SimpleNamespace(entry_id="entry", data={"owner_id": "alice"})
        manager.hass.config_entries = SimpleNamespace(
            async_entries=lambda domain: [config_entry] if domain == "habab_telegram" else [],
            async_reload=AsyncMock(),
        )

        def reloaded(entry_id):
            manager.entries[entry_id]["transport_identity"] = (
                "habab_relay", "https://habab-voice.wpmix.net", "fixture-service-key")
            return True

        manager.hass.config_entries.async_reload.side_effect = reloaded
        manager.hass.async_create_task = asyncio.create_task
        manager._provider_config_changed()
        await manager.provider_refresh_task
        manager.hass.config_entries.async_reload.assert_awaited_once_with("entry")

    async def test_provider_status_only_update_does_not_restart_direct_worker(self):
        manager = self.manager()
        manager.entries["entry"]["transport_identity"] = ("direct",)
        config_entry = SimpleNamespace(entry_id="entry", data={"owner_id": "alice"})
        manager.hass.config_entries = SimpleNamespace(
            async_entries=lambda domain: [config_entry] if domain == "habab_telegram" else [],
            async_reload=AsyncMock(return_value=True),
        )
        manager.hass.async_create_task = asyncio.create_task
        manager._provider_config_changed()
        await manager.provider_refresh_task
        manager.hass.config_entries.async_reload.assert_not_awaited()

    async def test_habab_provider_unload_closes_relay_and_loaded_event_restarts_it(self):
        manager = self.manager()
        manager.selected_provider_entry_id = "provider-a"
        manager.entries["entry"]["transport_identity"] = (
            "habab_relay", "https://habab-voice.wpmix.net", "fixture-service-key",
        )
        manager.entries["proxy"] = {"transport_identity": ("direct_proxy",)}
        provider_a = SimpleNamespace(
            entry_id="provider-a", domain="habab_home", data={"provider": "habab"},
            options={"assistant_selected": True}, state=SimpleNamespace(value="loaded"),
        )
        provider_b = SimpleNamespace(
            entry_id="provider-b", domain="habab_home", data={"provider": "habab"},
            options={"assistant_selected": False}, state=SimpleNamespace(value="not_loaded"),
        )
        telegram_entries = [
            SimpleNamespace(entry_id="entry", data={"owner_id": "alice"}),
            SimpleNamespace(entry_id="proxy", data={"owner_id": "bob", "proxy_url": "http://proxy.test"}),
        ]
        manager.hass.config_entries = SimpleNamespace(
            async_entries=lambda domain: (
                [provider_a, provider_b] if domain == "habab_home" else telegram_entries
            ),
            async_reload=AsyncMock(return_value=True),
        )
        manager.hass.async_create_task = asyncio.create_task

        async def close_entry(entry_id):
            manager.entries.pop(entry_id, None)

        with patch.object(manager, "close_entry", new=AsyncMock(side_effect=close_entry)) as close:
            self.assertIs(effective_provider_entry([provider_a, provider_b]), provider_a)
            manager._provider_entry_changed(ConfigEntryChange.UPDATED, provider_b)
            self.assertIsNone(manager.provider_refresh_task)
            self.assertEqual(manager.unavailable_provider_entries, set())
            close.assert_not_awaited()

            provider_a.state = SimpleNamespace(value="not_loaded")
            manager._provider_entry_changed(ConfigEntryChange.UPDATED, provider_a)
            await manager.provider_refresh_task

            close.assert_awaited_once_with("entry")
            self.assertNotIn("entry", manager.entries)
            self.assertIn("proxy", manager.entries)
            self.assertEqual(manager.transport_errors["entry"], "provider_unavailable")
            manager.hass.config_entries.async_reload.assert_not_awaited()

            provider_a.state = SimpleNamespace(value="loaded")
            manager._provider_entry_changed(ConfigEntryChange.UPDATED, provider_a)
            await manager.provider_refresh_task

        manager.hass.config_entries.async_reload.assert_awaited_once_with("entry")

    async def test_removed_habab_provider_keeps_relay_stopped_without_direct_fallback(self):
        manager = self.manager()
        manager.entries["entry"]["transport_identity"] = (
            "habab_relay", "https://habab-voice.wpmix.net", "fixture-service-key",
        )
        manager.selected_provider_entry_id = "provider-a"
        provider_a = SimpleNamespace(
            entry_id="provider-a", domain="habab_home", data={"provider": "habab"},
            options={"assistant_selected": True}, state=SimpleNamespace(value="not_loaded"),
        )
        provider_b = SimpleNamespace(
            entry_id="provider-b", domain="habab_home", data={"provider": "deepseek"},
            options={"assistant_selected": False}, state=SimpleNamespace(value="loaded"),
        )
        telegram_entry = SimpleNamespace(entry_id="entry", data={"owner_id": "alice"})
        manager.hass.config_entries = SimpleNamespace(
            async_entries=lambda domain: [provider_b] if domain == "habab_home" else [telegram_entry],
            async_reload=AsyncMock(return_value=True),
        )
        manager.hass.async_create_task = asyncio.create_task
        unsubscribe = Mock()
        manager.provider_entry_unsubscribers["provider-a"] = unsubscribe

        async def close_entry(entry_id):
            manager.entries.pop(entry_id, None)

        with patch.object(manager, "close_entry", new=AsyncMock(side_effect=close_entry)) as close:
            manager._provider_entry_changed(ConfigEntryChange.REMOVED, provider_a)
            await manager.provider_refresh_task

        close.assert_awaited_once_with("entry")
        unsubscribe.assert_called_once_with()
        self.assertNotIn("entry", manager.entries)
        self.assertEqual(manager.transport_errors["entry"], "provider_unavailable")
        self.assertEqual(manager.unavailable_provider_entries, {"provider-a"})
        manager.hass.config_entries.async_reload.assert_not_awaited()

        # Falling back to an unselected legacy provider after removal is blocked.
        manager._provider_entry_changed(ConfigEntryChange.UPDATED, provider_b)
        self.assertEqual(manager.unavailable_provider_entries, {"provider-a"})
        manager.hass.config_entries.async_reload.assert_not_awaited()

        # An explicit provider selection is an authorized route change.
        provider_b.options["assistant_selected"] = True
        manager._provider_entry_changed(ConfigEntryChange.UPDATED, provider_b)
        await manager.provider_refresh_task
        self.assertEqual(manager.selected_provider_entry_id, "provider-b")
        self.assertEqual(manager.unavailable_provider_entries, set())
        manager.hass.config_entries.async_reload.assert_awaited_once_with("entry")

    def group_entry(self):
        entry = SimpleNamespace(entry_id="entry", data={
            "owner_id": "alice", "bot_username": "testbot", "bot_id": 99,
        })
        return entry

    def bind_owner(self, manager, entry):
        manager.hass.config_entries = SimpleNamespace(async_entries=lambda _domain: [entry])
        manager.bindings[entry.entry_id] = {"chat_id": "10", "user_id": "20", "bot_id": 99}
        manager.entries[entry.entry_id].update(chat_id="10", user_id="20", ready=True, username="testbot")

    def bind_group(self, manager, entry):
        binding = {
            "chat_id": "-200", "title": "Home", "bot_id": 99, "owner_user_id": 20,
        }
        manager.group_bindings[entry.entry_id] = binding
        return binding

    async def dispatch_queued_message(self, manager, entry, queue, message):
        reader = await self.dispatch_worker_update(manager, entry, message)
        try:
            await asyncio.wait_for(queue.join(), timeout=1)
            turn = manager.turn_tasks.get(manager._turn_key(entry.entry_id, message["chat_id"]))
            if turn:
                await asyncio.wait_for(asyncio.gather(turn, return_exceptions=True), timeout=1)
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    async def dispatch_worker_update(self, manager, entry, message):
        manager.queues.setdefault(entry.entry_id, asyncio.Queue(maxsize=3))
        line = (json.dumps({"type": "message", "update_id": message["message_id"], **message}) + "\n").encode()
        received = asyncio.Event()

        class Stdout:
            def __init__(self):
                self.pending = line

            async def readline(self):
                if self.pending is not None:
                    pending, self.pending = self.pending, None
                    return pending
                received.set()
                await asyncio.Event().wait()

        state = manager.entries[entry.entry_id]
        state["process"] = SimpleNamespace(
            stdout=Stdout(), wait=AsyncMock(return_value=0), pid=123)
        reader = asyncio.create_task(manager._reader(entry, state))
        try:
            await asyncio.wait_for(received.wait(), timeout=1)
        except Exception:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            raise
        return reader

    async def dispatch_group_control(self, manager, entry, message):
        reader = await self.dispatch_worker_update(manager, entry, message)
        try:
            tasks = tuple(manager.control_tasks.get(entry.entry_id, ()))
            if not tasks:
                self.assertTrue(any(record["phase"] == "done" for record in manager.inbox.records.values()))
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=1)
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    async def test_unpaired_private_message_gets_safe_onboarding_without_runtime(self):
        manager = self.manager()
        manager.entries["entry"].update(chat_id=None, user_id=None)
        entry = self.group_entry()

        await manager.handle_one(entry, {
            "chat_type": "private", "chat_id": 55, "user_id": 55,
            "message_id": 1, "text": "/help",
        })

        manager.send.assert_awaited_once_with("entry", 55, BOT_ONBOARDING_MESSAGE)
        assistant = manager.hass.data["habab_home"]["assistant"]
        assistant.telegram_session.assert_not_awaited()
        assistant.run.assert_not_awaited()
        self.assertNotIn("entry", manager.bindings)

    async def test_owner_pairing_confirmation_and_help_explain_household_group_next_step(self):
        manager = self.manager()
        manager.entries["entry"].update(chat_id=None, user_id=None)
        entry = self.group_entry()
        manager.hass.config_entries = SimpleNamespace(async_entries=lambda _domain: [entry])
        nonce = (await manager.pair("alice"))["pairing_url"].split("start=", 1)[1]

        await manager.handle_one(entry, {
            "chat_type": "private", "chat_id": 10, "user_id": 20,
            "message_id": 1, "text": f"/start {nonce}",
        })
        self.assertEqual(manager.send.await_args.args[2], OWNER_PAIRING_COMPLETE_MESSAGE)

        await manager.handle_one(entry, {
            "chat_type": "private", "chat_id": 10, "user_id": 20,
            "message_id": 2, "text": "/help",
        })
        help_text = manager.send.await_args.args[2]
        self.assertIn("Моя дача", help_text)
        self.assertIn("/new", help_text)
        self.assertIn("/cancel", help_text)
        assistant = manager.hass.data["habab_home"]["assistant"]
        assistant.telegram_session.assert_not_awaited()
        assistant.run.assert_not_awaited()

    async def test_group_link_requires_owner_dm_and_nonce_owner_claims_exact_group(self):
        manager = self.manager()
        entry = self.group_entry()
        self.bind_owner(manager, entry)
        manager.get_chat_member.return_value = {"status": "administrator", "is_member": True}
        link = await manager.group_pair("alice")
        self.assertTrue(link["group_pairing_url"].startswith("https://t.me/testbot?startgroup="))
        nonce = link["group_pairing_url"].split("startgroup=", 1)[1]

        self.assertFalse(await manager._claim_message(entry, {
            "chat_type": "supergroup", "chat_id": -200, "user_id": 20,
            "message_id": 1, "text": f"/start@testbot {nonce}x", "chat_title": "Untrusted",
        }))
        self.assertFalse(await manager._claim_message(entry, {
            "chat_type": "supergroup", "chat_id": -200, "user_id": 21,
            "message_id": 2, "text": f"/start@testbot {nonce}", "chat_title": "Untrusted",
        }))
        self.assertFalse(await manager._claim_message(entry, {
            "chat_type": "supergroup", "chat_id": -200, "user_id": 20,
            "message_id": 3, "text": f"/start@testbot {nonce}", "chat_title": "Our home",
        }))
        self.assertEqual(manager.group_bindings["entry"], {
            "chat_id": "-200", "title": "Our home", "bot_id": 99, "owner_user_id": 20,
        })
        self.assertNotIn("entry", manager.group_pairing)
        manager.group_bindings_store.async_save.assert_awaited_once()
        self.assertTrue(await manager._is_group_member(entry, -200, 20, manager.group_bindings["entry"]))
        status = await manager.status("alice")
        self.assertEqual(status["group"], {"phase": "connected", "title": "Our home"})
        encoded = json.dumps(status)
        self.assertNotIn(nonce, encoded)
        self.assertNotIn("-200", encoded)

    async def test_group_claim_waits_for_bot_admin_and_api_errors_fail_closed(self):
        manager = self.manager()
        entry = self.group_entry()
        self.bind_owner(manager, entry)
        link = await manager.group_pair("alice")
        nonce = link["group_pairing_url"].split("startgroup=", 1)[1]
        manager.get_chat_member.return_value = {"status": "member", "is_member": True}
        message = {"chat_type": "group", "chat_id": -200, "user_id": 20,
                   "message_id": 4, "text": f"/start@testbot {nonce}", "chat_title": "Dacha"}
        await manager._claim_message(entry, message)
        self.assertNotIn("entry", manager.group_bindings)
        status = await manager.group_status("alice", entry)
        self.assertEqual(status["phase"], "awaiting_admin")
        self.assertEqual(status["error"], "bot_not_admin")

        manager.get_chat_member.return_value = None
        link2 = await manager.group_pair("alice")
        nonce2 = link2["group_pairing_url"].split("startgroup=", 1)[1]
        await manager._claim_message(entry, {**message, "message_id": 5, "text": f"/start@testbot {nonce2}"})
        self.assertNotIn("entry", manager.group_bindings)
        self.assertEqual(manager.group_candidates["entry"]["error"], "verification_unavailable")
        self.assertFalse(await manager._is_group_member(entry, -200, 20))

    async def test_terminal_worker_poll_errors_clear_readiness_in_status(self):
        class Stdout:
            def __init__(self, lines, wait_after_lines=False):
                self.lines = list(lines)
                self.wait_after_lines = wait_after_lines
                self.waiting = asyncio.Event()

            async def readline(self):
                if self.lines:
                    return self.lines.pop(0)
                if self.wait_after_lines:
                    self.waiting.set()
                    await asyncio.Event().wait()
                return b""

        class Process:
            def __init__(self, lines, wait_after_lines=False):
                self.stdout = Stdout(lines, wait_after_lines)
                self.returncode = 0
                self.wait = AsyncMock(return_value=0)

        ready = b'{"type":"ready","username":"testbot"}\n'
        conflict = b'{"type":"error","operation":"poll","code":"telegram_conflict","fatal":true}\n'
        entry = self.group_entry()

        manager = self.manager()
        self.bind_owner(manager, entry)
        manager.entries["entry"]["process"] = Process([ready, conflict], wait_after_lines=True)
        manager.queues["entry"] = asyncio.Queue(maxsize=5)
        reader = asyncio.create_task(manager._reader(entry, manager.entries["entry"]))
        await asyncio.wait_for(manager.entries["entry"]["process"].stdout.waiting.wait(), timeout=1)
        self.assertEqual(manager.entries["entry"]["error"], "worker_error")
        self.assertFalse(manager.entries["entry"]["ready"])
        self.assertEqual((await manager.status("alice"))["phase"], "error")
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)

        stopped_manager = self.manager()
        self.bind_owner(stopped_manager, entry)
        stopped_manager.entries["entry"]["process"] = Process([ready])
        stopped_manager.queues["entry"] = asyncio.Queue(maxsize=5)
        await stopped_manager._reader(entry, stopped_manager.entries["entry"])
        self.assertEqual(stopped_manager.entries["entry"]["error"], "worker_stopped")
        self.assertFalse(stopped_manager.entries["entry"]["ready"])
        self.assertEqual((await stopped_manager.status("alice"))["phase"], "error")

    async def test_bot_admin_update_finishes_pending_group_without_repeating_nonce(self):
        class Stdout:
            def __init__(self, lines):
                self.lines = list(lines)
                self.waiting = asyncio.Event()
                self.release = asyncio.Event()

            async def readline(self):
                if self.lines:
                    return self.lines.pop(0)
                self.waiting.set()
                await self.release.wait()
                return b""

        class Process:
            def __init__(self, lines):
                self.stdout = Stdout(lines)
                self.returncode = 0
                self.wait = AsyncMock(return_value=0)

        manager = self.manager()
        entry = self.group_entry()
        self.bind_owner(manager, entry)
        link = await manager.group_pair("alice")
        nonce = link["group_pairing_url"].split("startgroup=", 1)[1]
        manager.get_chat_member.return_value = {"status": "member", "is_member": True}
        await manager._claim_message(entry, {
            "chat_type": "group", "chat_id": -200, "user_id": 20,
            "message_id": 1, "text": f"/start@testbot {nonce}", "chat_title": "Home",
        })
        self.assertNotIn("entry", manager.group_bindings)
        self.assertEqual(manager.group_candidates["entry"]["error"], "bot_not_admin")

        manager.get_chat_member.reset_mock()
        manager.get_chat_member.return_value = {"status": "administrator", "is_member": True}
        manager.entries["entry"]["process"] = Process([
            b'{"type":"bot_member_update","chat_id":-200,"status":"administrator"}\n',
        ])
        reader = asyncio.create_task(manager._reader(entry, manager.entries["entry"]))
        try:
            await asyncio.wait_for(manager.entries["entry"]["process"].stdout.waiting.wait(), timeout=1)
            candidate_task = manager.group_candidate_tasks.get("entry")
            if candidate_task:
                await candidate_task
        finally:
            manager.entries["entry"]["process"].stdout.release.set()
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        self.assertEqual(manager.group_bindings["entry"], {
            "chat_id": "-200", "title": "Home", "bot_id": 99, "owner_user_id": 20,
        })
        self.assertNotIn("entry", manager.group_pairing)
        self.assertNotIn("entry", manager.group_candidates)
        manager.get_chat_member.assert_awaited_once_with("entry", -200, 99)
        self.assertIn("Группа дома подключена", manager.send.await_args.args[2])

    async def test_reader_correlates_member_reply_while_group_message_auth_is_awaiting(self):
        class FakeStdin:
            def __init__(self, stdout):
                self.stdout = stdout
                self.commands = []

            def write(self, raw):
                command = json.loads(raw)
                self.commands.append(command)
                if command["type"] == "get_chat_member":
                    result = {
                        "type": "member", "request_id": command["request_id"],
                        "status": "administrator", "is_member": True,
                    }
                elif command["type"] == "send":
                    result = {"type": "sent", "request_id": command["request_id"]}
                else:
                    return
                self.stdout.feed_data((json.dumps(result) + "\n").encode())

            async def drain(self):
                await asyncio.sleep(0)

        class Process:
            def __init__(self):
                self.stdout = asyncio.StreamReader()
                self.stdin = FakeStdin(self.stdout)
                self.returncode = None
                self.wait = AsyncMock(return_value=0)

        manager = self.manager()
        entry = self.group_entry()
        self.bind_owner(manager, entry)
        link = await manager.group_pair("alice")
        nonce = link["group_pairing_url"].split("startgroup=", 1)[1]
        process = Process()
        manager.entries["entry"]["process"] = process
        manager.get_chat_member = TelegramManager.get_chat_member.__get__(manager)
        manager._write = TelegramManager._write.__get__(manager)
        manager.queues["entry"] = asyncio.Queue(maxsize=5)
        bound = asyncio.Event()
        manager.group_bindings_store.async_save.side_effect = lambda _data: bound.set()
        reader = asyncio.create_task(manager._reader(entry, manager.entries["entry"]))
        consumer = asyncio.create_task(manager._consume(entry))

        process.stdout.feed_data((json.dumps({
            "type": "message", "update_id": 1, "chat_id": -200, "user_id": 20, "message_id": 1,
            "chat_type": "supergroup", "text": f"/start@testbot {nonce}",
            "chat_title": "Home",
        }) + "\n").encode())
        try:
            await asyncio.wait_for(bound.wait(), timeout=1)
        except TimeoutError:
            self.fail(f"worker commands={process.stdin.commands!r}; "
                      f"candidate={manager.group_candidates.get('entry')!r}; "
                      f"state_error={manager.entries['entry'].get('error')!r}; "
                      f"queue={manager.queues['entry'].qsize()}")
        await asyncio.wait_for(manager.queues["entry"].join(), timeout=1)
        self.assertEqual(manager.group_bindings["entry"]["chat_id"], "-200")
        self.assertEqual(manager.pending_members, {})
        self.assertIn("get_chat_member", [command["type"] for command in process.stdin.commands])

        reader.cancel()
        consumer.cancel()
        await asyncio.gather(reader, consumer, return_exceptions=True)

    async def test_inactive_owner_cannot_issue_group_link_or_keep_member_access(self):
        manager = self.manager()
        entry = self.group_entry()
        self.bind_owner(manager, entry)
        manager.group_bindings["entry"] = {
            "chat_id": "-200", "title": "Home", "bot_id": 99, "owner_user_id": 20,
        }
        manager.hass.auth.async_get_user.return_value = SimpleNamespace(is_active=False, is_admin=True)
        with self.assertRaisesRegex(ValueError, "^not_allowed$"):
            await manager.group_pair("alice")
        accepted = await manager._claim_message(entry, {
            "chat_type": "private", "chat_id": 50, "user_id": 50,
            "message_id": 9, "text": "hello",
        })
        self.assertFalse(accepted)
        self.assertNotIn("entry", manager.group_bindings)
        manager.hass.data["habab_home"]["assistant"].telegram_session.assert_not_awaited()
        manager.hass.data["habab_home"]["assistant"].run.assert_not_awaited()

    async def test_member_dm_requires_live_bot_admin_and_member_status(self):
        manager = self.manager()
        entry = self.group_entry()
        self.bind_owner(manager, entry)
        manager.group_bindings["entry"] = {
            "chat_id": "-200", "title": "Home", "bot_id": 99, "owner_user_id": 20,
        }
        manager.get_chat_member.side_effect = lambda _entry, _group, user: (
            {"status": "administrator", "is_member": True} if user == 99 else
            {"status": "member", "is_member": True} if user == 50 else
            {"status": "left", "is_member": False})

        await manager.handle_one(entry, {"chat_type": "private", "chat_id": 50,
            "user_id": 50, "message_id": 1, "text": "/help"})
        assistant = manager.hass.data["habab_home"]["assistant"]
        self.assertTrue(assistant.telegram_forget.await_count == 0)
        self.assertIn("entry:50:1", manager.dedupe)
        self.assertIn("/new", manager.send.await_args.args[2])

        manager.get_chat_member.reset_mock(side_effect=True)
        manager.get_chat_member.side_effect = lambda _entry, _group, user: (
            {"status": "member", "is_member": True} if user == 99 else
            {"status": "left", "is_member": False})
        await manager.handle_one(entry, {"chat_type": "private", "chat_id": 51,
            "user_id": 51, "message_id": 2, "text": "/help"})
        self.assertEqual(manager.send.await_args.args[2], UNKNOWN_MEMBER_MESSAGE)
        assistant.telegram_session.assert_not_awaited()
        assistant.run.assert_not_awaited()

    async def test_member_leave_during_runtime_suppresses_private_completion(self):
        manager = self.manager()
        entry = self.group_entry()
        self.bind_owner(manager, entry)
        binding = {"chat_id": "-200", "title": "Home", "bot_id": 99,
                   "owner_user_id": 20}
        manager.group_bindings["entry"] = binding
        assistant = manager.hass.data["habab_home"]["assistant"]
        assistant.telegram_sessions["alice:entry:50"] = "member-session"
        assistant.telegram_session.return_value = {"session_id": "member-session"}
        assistant.run.return_value = {"messages": [{"role": "assistant", "text": "answer"}]}
        membership_checks = 0

        async def get_member(_entry_id, _chat_id, user_id):
            nonlocal membership_checks
            if user_id == 99:
                return {"status": "administrator", "is_member": True}
            membership_checks += 1
            if membership_checks <= 3:
                return {"status": "member", "is_member": True}
            return {"status": "left", "is_member": False}

        manager.get_chat_member.side_effect = get_member
        manager.typing = AsyncMock(return_value=True)

        await manager._run_message(entry, "50", 50, "question", binding)

        self.assertEqual(membership_checks, 4)
        assistant.run.assert_awaited_once()
        manager.send.assert_not_awaited()
        assistant.telegram_forget.assert_awaited_once_with("alice", "entry", "50")

    async def test_cancel_epoch_suppresses_completion_while_cancel_auth_is_waiting(self):
        manager = self.manager()
        entry = self.group_entry()
        self.bind_owner(manager, entry)
        binding = {"chat_id": "-200", "title": "Home", "bot_id": 99,
                   "owner_user_id": 20}
        manager.group_bindings["entry"] = binding
        assistant = manager.hass.data["habab_home"]["assistant"]
        assistant.telegram_sessions["alice:entry:50"] = "member-session"
        assistant.telegram_session.return_value = {"session_id": "member-session"}
        runtime_started = asyncio.Event()
        runtime_release = asyncio.Event()
        cancel_auth_started = asyncio.Event()
        cancel_auth_release = asyncio.Event()
        cancel_task = None

        async def get_member(_entry_id, _chat_id, user_id):
            if user_id == 99:
                return {"status": "administrator", "is_member": True}
            if asyncio.current_task() is cancel_task:
                cancel_auth_started.set()
                await cancel_auth_release.wait()
            return {"status": "member", "is_member": True}

        async def run(*_args):
            runtime_started.set()
            await runtime_release.wait()
            return {"messages": [{"role": "assistant", "text": "answer"}]}

        manager.get_chat_member.side_effect = get_member
        assistant.run.side_effect = run
        manager.typing = AsyncMock(return_value=True)

        turn_key = manager._turn_key(entry.entry_id, "50")
        turn_task = asyncio.create_task(
            manager._run_message(entry, "50", 50, "question", binding, 0))
        manager.turn_tasks[turn_key] = turn_task
        await asyncio.wait_for(runtime_started.wait(), timeout=1)

        cancel = {"chat_type": "private", "chat_id": 50, "user_id": 50,
                  "message_id": 101, "text": "/cancel", "_habab_claimed": True}
        manager._advance_cancel_epoch(entry, cancel)
        cancel_task = asyncio.create_task(manager.handle_one(entry, cancel))
        await asyncio.wait_for(cancel_auth_started.wait(), timeout=1)
        runtime_release.set()
        await asyncio.wait_for(turn_task, timeout=1)
        self.assertNotIn("answer", [call.args[2] for call in manager.send.await_args_list])

        cancel_auth_release.set()
        await asyncio.wait_for(cancel_task, timeout=1)
        self.assertEqual(manager.send.await_args_list[-1].args,
                         ("entry", "50", "Остановлено."))

    async def test_queued_failure_sends_generic_error_for_current_owner_epoch(self):
        manager = self.manager()
        entry = self.group_entry()
        self.bind_owner(manager, entry)
        manager.queues["entry"] = asyncio.Queue(maxsize=2)
        manager.pending_message_ids["entry"] = {"10": {100}}
        manager.queues["entry"].put_nowait({
            "chat_type": "private", "chat_id": 10, "user_id": 20,
            "message_id": 100, "text": "queued task", "_habab_cancel_epoch": 0,
        })
        manager.handle_one = AsyncMock(side_effect=RuntimeError("fixture failure"))

        consumer = asyncio.create_task(manager._consume(entry))
        try:
            await asyncio.wait_for(manager.queues["entry"].join(), timeout=1)
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

        manager.hass.auth.async_get_user.assert_awaited_once_with("alice")
        manager.send.assert_awaited_once_with(
            "entry", "10", "Не удалось обработать сообщение.")

    async def test_queued_failure_does_not_send_fallback_after_cancel_epoch_changes(self):
        manager = self.manager()
        entry = self.group_entry()
        self.bind_owner(manager, entry)
        manager.queues["entry"] = asyncio.Queue(maxsize=2)
        manager.pending_message_ids["entry"] = {"10": {100}}
        manager.queues["entry"].put_nowait({
            "chat_type": "private", "chat_id": 10, "user_id": 20,
            "message_id": 100, "text": "queued task", "_habab_cancel_epoch": 0,
        })
        started = asyncio.Event()
        release = asyncio.Event()
        handle_one = TelegramManager.handle_one.__get__(manager, TelegramManager)

        async def fail_queued_but_handle_control(entry_arg, message):
            if message.get("text") == "queued task":
                started.set()
                await release.wait()
                raise RuntimeError("fixture failure")
            await handle_one(entry_arg, message)

        manager.handle_one = fail_queued_but_handle_control
        consumer = asyncio.create_task(manager._consume(entry))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            cancel = {
                "chat_type": "private", "chat_id": 10, "user_id": 20,
                "message_id": 101, "text": "/cancel",
            }
            manager._advance_cancel_epoch(entry, cancel)
            manager._schedule_control(entry, cancel)
            controls = manager.control_tasks["entry"]
            await asyncio.wait_for(asyncio.gather(*tuple(controls)), timeout=1)
            release.set()
            await asyncio.wait_for(manager.queues["entry"].join(), timeout=1)
        finally:
            release.set()
            controls = tuple(manager.control_tasks.get("entry", ()))
            for control in controls:
                if not control.done():
                    control.cancel()
            consumer.cancel()
            await asyncio.gather(consumer, *controls, return_exceptions=True)

        self.assertEqual(manager.hass.auth.async_get_user.await_count, 3)
        manager.send.assert_awaited_once_with("entry", "10", "Остановлено.")

    async def test_member_origin_rechecks_current_group_membership_on_every_lookup(self):
        manager = self.manager()
        entry = self.group_entry()
        self.bind_owner(manager, entry)
        manager.group_bindings["entry"] = {
            "chat_id": "-200", "title": "Home", "bot_id": 99, "owner_user_id": 20,
        }
        assistant = manager.hass.data["habab_home"]["assistant"]
        assistant.telegram_sessions["alice:entry:50"] = "member-session"
        manager.group_member_chats["entry"] = {"50": 50}
        manager.get_chat_member.side_effect = lambda _entry, _group, user: (
            {"status": "administrator", "is_member": True} if user == 99 else
            {"status": "member", "is_member": True})

        origin = await manager.member_origin("alice", "entry", "50", "member-session")
        self.assertEqual(origin, {
            "channel": "telegram", "owner": "alice", "entry_id": "entry", "chat_id": "50",
            "bot_id": 99, "group_id": "-200", "user_id": 50,
        })
        self.assertEqual(manager.get_chat_member.await_count, 2)
        manager.get_chat_member.reset_mock(side_effect=True)
        manager.get_chat_member.side_effect = lambda _entry, _group, user: (
            {"status": "administrator", "is_member": True} if user == 99 else
            {"status": "left", "is_member": False})
        self.assertIsNone(await manager.member_origin("alice", "entry", "50", "member-session"))
        self.assertEqual(manager.get_chat_member.await_count, 2)

    async def test_revoked_member_is_forgotten_and_member_leave_cancels_inflight_turn(self):
        manager = self.manager()
        entry = self.group_entry()
        self.bind_owner(manager, entry)
        manager.group_bindings["entry"] = {
            "chat_id": "-200", "title": "Home", "bot_id": 99, "owner_user_id": 20,
        }
        assistant = manager.hass.data["habab_home"]["assistant"]
        assistant.telegram_sessions["alice:entry:50"] = "member-session"
        manager.group_member_chats["entry"] = {"50": 50}
        cancelled = asyncio.Event()

        async def turn():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        manager.turn_tasks["entry:50"] = asyncio.create_task(turn())
        manager._write = AsyncMock()
        manager.entries["entry"]["process"] = SimpleNamespace(
            stdout=SimpleNamespace(readline=AsyncMock(side_effect=[
                b'{"type":"member_update","chat_id":-200,"user_id":50,"status":"left","is_member":false,"chat_type":"supergroup"}\n',
                b"",
            ])), returncode=0, wait=AsyncMock(return_value=0))
        manager.hass.async_create_task = asyncio.create_task
        await manager._reader(entry, manager.entries["entry"])
        await asyncio.gather(*manager.control_tasks["entry"], return_exceptions=True)
        self.assertTrue(cancelled.is_set())
        assistant.telegram_forget.assert_awaited_once_with("alice", "entry", "50")

    async def test_member_cancel_skips_their_already_queued_message(self):
        manager = self.manager()
        entry = self.group_entry()
        self.bind_owner(manager, entry)
        manager.group_bindings["entry"] = {
            "chat_id": "-200", "title": "Home", "bot_id": 99, "owner_user_id": 20,
        }
        manager.get_chat_member.side_effect = lambda _entry, _group, user: {
            "status": "administrator" if user == 99 else "member", "is_member": True,
        }
        manager.queues["entry"] = asyncio.Queue(maxsize=3)
        queued = {"chat_type": "private", "chat_id": 50, "user_id": 50,
                  "message_id": 100, "text": "run this", "_habab_claimed": True}
        manager.queues["entry"].put_nowait(queued)
        manager.pending_message_ids["entry"] = {"50": {100}}
        await manager.handle_one(entry, {"chat_type": "private", "chat_id": 50,
            "user_id": 50, "message_id": 101, "text": "/cancel"})
        self.assertEqual(manager.send.await_args.args[2], "Остановлено.")

        consumer = asyncio.create_task(manager._consume(entry))
        await manager.queues["entry"].join()
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        assistant = manager.hass.data["habab_home"]["assistant"]
        assistant.telegram_session.assert_not_awaited()
        assistant.run.assert_not_awaited()

    async def test_cancel_epoch_invalidates_message_dequeued_during_membership_check(self):
        manager = self.manager()
        entry = self.group_entry()
        self.bind_owner(manager, entry)
        manager.group_bindings["entry"] = {
            "chat_id": "-200", "title": "Home", "bot_id": 99, "owner_user_id": 20,
        }
        started = asyncio.Event()
        release = asyncio.Event()
        consumer = None

        async def authorize(_entry, _group_id, _user_id, _expected_binding=None):
            if asyncio.current_task() is consumer:
                started.set()
                await release.wait()
            return True

        manager._is_group_member = AsyncMock(side_effect=authorize)
        manager.queues["entry"] = asyncio.Queue(maxsize=3)
        queued = {"chat_type": "private", "chat_id": 50, "user_id": 50,
                  "message_id": 100, "text": "run this", "_habab_claimed": True,
                  "_habab_cancel_epoch": 0}
        manager.queues["entry"].put_nowait(queued)
        manager.pending_message_ids["entry"] = {"50": {100}}

        consumer = asyncio.create_task(manager._consume(entry))
        await asyncio.wait_for(started.wait(), timeout=1)
        cancel = {"chat_type": "private", "chat_id": 50, "user_id": 50,
                  "message_id": 101, "text": "/cancel", "_habab_claimed": True}
        manager._advance_cancel_epoch(entry, cancel)
        await manager.handle_one(entry, cancel)
        self.assertEqual(manager.send.await_args.args[2], "Остановлено.")
        release.set()
        await manager.queues["entry"].join()
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)

        assistant = manager.hass.data["habab_home"]["assistant"]
        assistant.telegram_session.assert_not_awaited()
        assistant.run.assert_not_awaited()

    async def test_group_unlink_revokes_member_history_but_keeps_owner_dm_binding(self):
        manager = self.manager()
        entry = self.group_entry()
        self.bind_owner(manager, entry)
        manager.group_bindings["entry"] = {
            "chat_id": "-200", "title": "Home", "bot_id": 99, "owner_user_id": 20,
        }
        assistant = manager.hass.data["habab_home"]["assistant"]
        assistant.telegram_sessions.update({
            "alice:entry:10": "owner-session", "alice:entry:50": "member-session",
            "alice:other-entry:60": "other-session",
        })

        async def forget(owner, entry_id, chat_id):
            return assistant.telegram_sessions.pop(f"{owner}:{entry_id}:{chat_id}", None)

        assistant.telegram_forget.side_effect = forget
        result = await manager.unlink_group("alice")
        self.assertEqual(result, {"unlinked": True})
        self.assertNotIn("entry", manager.group_bindings)
        self.assertEqual(manager.group_bindings_store.async_save.await_args.args[0], {})
        self.assertEqual(assistant.telegram_forget.await_args.args, ("alice", "entry", "50"))
        self.assertEqual(assistant.telegram_sessions, {
            "alice:entry:10": "owner-session", "alice:other-entry:60": "other-session",
        })

    async def test_each_group_member_keeps_a_separate_private_assistant_history(self):
        manager = self.manager()
        entry = self.group_entry()
        self.bind_owner(manager, entry)
        manager.group_bindings["entry"] = {
            "chat_id": "-200", "title": "Home", "bot_id": 99, "owner_user_id": 20,
        }
        assistant = manager.hass.data["habab_home"]["assistant"]
        async def telegram_session(owner, entry_id, chat_id):
            session_id = f"member-{chat_id}"
            assistant.telegram_sessions[f"{owner}:{entry_id}:{chat_id}"] = session_id
            return {"session_id": session_id}

        assistant.telegram_session.side_effect = telegram_session
        assistant.run.return_value = {"messages": [{"role": "assistant", "text": "done"}]}
        manager.get_chat_member.side_effect = lambda _entry, _group, user: (
            {"status": "administrator", "is_member": True} if user == 99 else
            {"status": "member", "is_member": True})
        manager.typing = AsyncMock(return_value=True)

        await asyncio.gather(
            manager._run_message(entry, "50", 50, "first request", manager.group_bindings["entry"]),
            manager._run_message(entry, "51", 51, "second request", manager.group_bindings["entry"]),
        )

        self.assertEqual(assistant.telegram_session.await_args_list[0].args,
                         ("alice", "entry", "50"))
        self.assertEqual(assistant.telegram_session.await_args_list[1].args,
                         ("alice", "entry", "51"))
        self.assertEqual(assistant.run.await_count, 2)
        self.assertEqual({call.args[2] for call in assistant.run.await_args_list},
                         {"member-50", "member-51"})
        self.assertEqual({call.args[-1] for call in assistant.run.await_args_list}, {"50", "51"})

    async def test_owner_and_member_group_messages_share_bound_group_session_and_reply_there(self):
        manager = self.manager()
        entry = self.group_entry()
        self.bind_owner(manager, entry)
        self.bind_group(manager, entry)
        assistant = manager.hass.data["habab_home"]["assistant"]
        manager.queues[entry.entry_id] = asyncio.Queue(maxsize=3)
        queue = manager.queues[entry.entry_id]
        manager.typing = AsyncMock(return_value=True)
        manager.get_chat_member.side_effect = lambda _entry, _group, user: (
            {"status": "administrator", "is_member": True} if user == 99 else
            {"status": "creator", "is_member": True} if user == 20 else
            {"status": "member", "is_member": True})

        async def get_group_session(owner, entry_id, chat_id):
            session_id = "shared-group-session"
            assistant.telegram_sessions[f"{owner}:{entry_id}:{chat_id}"] = session_id
            return {"session_id": session_id}

        assistant.telegram_session.side_effect = get_group_session
        assistant.run.return_value = {"messages": [{"role": "assistant", "text": "read-only answer"}]}
        consumer = asyncio.create_task(manager._consume(entry))
        try:
            await self.dispatch_queued_message(manager, entry, queue, {
                "chat_type": "supergroup", "chat_id": -200, "user_id": 20,
                "message_id": 1, "text": "Owner read-only question",
            })
            await self.dispatch_queued_message(manager, entry, queue, {
                "chat_type": "supergroup", "chat_id": -200, "user_id": 50,
                "message_id": 2, "text": "Member read-only question",
            })
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

        self.assertEqual(assistant.telegram_session.await_args_list[0].args,
                         ("alice", "entry", "-200"))
        self.assertEqual(assistant.telegram_session.await_args_list[1].args,
                         ("alice", "entry", "-200"))
        self.assertEqual({call.args[2] for call in assistant.run.await_args_list},
                         {"shared-group-session"})
        self.assertEqual({call.args[-1] for call in assistant.run.await_args_list}, {"-200"})
        self.assertEqual(manager.send.await_args_list[0].args,
                         ("entry", "-200", "read-only answer"))
        self.assertEqual(manager.send.await_args_list[1].args,
                         ("entry", "-200", "read-only answer"))

    async def test_group_messages_reject_wrong_group_departed_member_and_unknown_command(self):
        manager = self.manager()
        entry = self.group_entry()
        self.bind_owner(manager, entry)
        self.bind_group(manager, entry)
        manager.queues[entry.entry_id] = asyncio.Queue(maxsize=3)
        queue = manager.queues[entry.entry_id]
        manager.get_chat_member.side_effect = lambda _entry, _group, user: (
            {"status": "administrator", "is_member": True} if user == 99 else
            {"status": "left", "is_member": False})
        consumer = asyncio.create_task(manager._consume(entry))
        try:
            for message in (
                {"chat_type": "supergroup", "chat_id": -201, "user_id": 20,
                 "message_id": 1, "text": "Wrong group"},
                {"chat_type": "supergroup", "chat_id": -201, "user_id": 20,
                 "message_id": 4, "text": "/cancel"},
                {"chat_type": "supergroup", "chat_id": -200, "user_id": 52,
                 "message_id": 2, "text": "Departed member"},
                {"chat_type": "supergroup", "chat_id": -200, "user_id": 20,
                 "message_id": 3, "text": "/unsupported"},
            ):
                await self.dispatch_queued_message(manager, entry, queue, message)
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

        manager.send.assert_not_awaited()
        assistant = manager.hass.data["habab_home"]["assistant"]
        assistant.telegram_session.assert_not_awaited()
        assistant.run.assert_not_awaited()
        self.assertNotIn("-201", manager.cancel_epochs.get(entry.entry_id, {}))
        self.assertFalse(manager._should_route_control(entry, manager.entries[entry.entry_id], {
            "chat_type": "supergroup", "chat_id": -201, "user_id": 20,
            "message_id": 4, "text": "/help",
        }))

    async def test_group_help_and_new_controls_reply_and_reset_only_bound_group_session(self):
        manager = self.manager()
        entry = self.group_entry()
        self.bind_owner(manager, entry)
        self.bind_group(manager, entry)
        manager.get_chat_member.side_effect = lambda _entry, _group, user: {
            "status": "administrator" if user == 99 else "creator",
            "is_member": True,
        }

        await self.dispatch_group_control(manager, entry, {
            "chat_type": "supergroup", "chat_id": -200, "user_id": 20,
            "message_id": 1, "text": "/help",
        })
        self.assertEqual(manager.send.await_args.args[:2], ("entry", "-200"))
        self.assertIn("/new", manager.send.await_args.args[2])
        self.assertIn("группе", manager.send.await_args.args[2])

        await self.dispatch_group_control(manager, entry, {
            "chat_type": "supergroup", "chat_id": -200, "user_id": 20,
            "message_id": 2, "text": "/new",
        })
        manager.hass.data["habab_home"]["assistant"].telegram_reset.assert_awaited_once_with(
            "alice", "entry", "-200")
        self.assertEqual(manager.send.await_args.args,
                         ("entry", "-200", "Начат новый диалог."))

    async def test_group_cancel_cancels_active_shared_turn_and_acknowledges_in_group(self):
        manager = self.manager()
        entry = self.group_entry()
        self.bind_owner(manager, entry)
        self.bind_group(manager, entry)
        assistant = manager.hass.data["habab_home"]["assistant"]
        manager.queues[entry.entry_id] = asyncio.Queue(maxsize=2)
        queue = manager.queues[entry.entry_id]
        manager.typing = AsyncMock(return_value=True)
        manager.get_chat_member.side_effect = lambda _entry, _group, user: {
            "status": "administrator" if user == 99 else "creator",
            "is_member": True,
        }
        run_started = asyncio.Event()

        async def run(*_args):
            run_started.set()
            await asyncio.Event().wait()
            return {"messages": [{"role": "assistant", "text": "must not be sent"}]}

        async def get_group_session(owner, entry_id, chat_id):
            assistant.telegram_sessions[f"{owner}:{entry_id}:{chat_id}"] = "shared-group-session"
            return {"session_id": "shared-group-session"}

        assistant.telegram_session.side_effect = get_group_session
        assistant.run.side_effect = run
        consumer = asyncio.create_task(manager._consume(entry))
        try:
            queue.put_nowait({
                "chat_type": "supergroup", "chat_id": -200, "user_id": 20,
                "message_id": 1, "text": "Start shared turn",
            })
            await asyncio.wait_for(run_started.wait(), timeout=1)
            await self.dispatch_group_control(manager, entry, {
                "chat_type": "supergroup", "chat_id": -200, "user_id": 20,
                "message_id": 2, "text": "/cancel",
            })
            await asyncio.wait_for(queue.join(), timeout=1)
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

        manager.send.assert_awaited_once_with("entry", "-200", "Остановлено.")
        assistant.run.assert_awaited_once()
        assistant.abort.assert_not_awaited()

    async def test_group_cancel_registers_before_member_api_wait_and_suppresses_completion(self):
        manager = self.manager()
        entry = self.group_entry()
        self.bind_owner(manager, entry)
        self.bind_group(manager, entry)
        assistant = manager.hass.data["habab_home"]["assistant"]
        manager.queues[entry.entry_id] = asyncio.Queue(maxsize=2)
        manager.typing = AsyncMock(return_value=True)
        runtime_started = asyncio.Event()
        runtime_release = asyncio.Event()
        cancel_auth_started = asyncio.Event()
        cancel_auth_release = asyncio.Event()

        async def get_member(_entry_id, _group_id, user):
            if user == 99:
                return {"status": "administrator", "is_member": True}
            control = manager.control_lanes.get(entry.entry_id, {}).get("-200")
            if asyncio.current_task() is control:
                cancel_auth_started.set()
                await cancel_auth_release.wait()
            return {"status": "creator", "is_member": True}

        async def get_group_session(owner, entry_id, chat_id):
            assistant.telegram_sessions[f"{owner}:{entry_id}:{chat_id}"] = "shared-group-session"
            return {"session_id": "shared-group-session"}

        async def run(*_args):
            runtime_started.set()
            await runtime_release.wait()
            return {"messages": [{"role": "assistant", "text": "must not be sent"}]}

        manager.get_chat_member.side_effect = get_member
        assistant.telegram_session.side_effect = get_group_session
        assistant.run.side_effect = run
        assistant.abort.return_value = True
        consumer = asyncio.create_task(manager._consume(entry))
        readers = []
        try:
            readers.append(await self.dispatch_worker_update(manager, entry, {
                "chat_type": "supergroup", "chat_id": -200, "user_id": 20,
                "message_id": 1, "text": "Start shared turn",
            }))
            await asyncio.wait_for(runtime_started.wait(), timeout=1)
            readers.append(await self.dispatch_worker_update(manager, entry, {
                "chat_type": "supergroup", "chat_id": -200, "user_id": 20,
                "message_id": 2, "text": "/cancel",
            }))
            await asyncio.wait_for(cancel_auth_started.wait(), timeout=1)
            self.assertNotIn("-200", manager.cancel_epochs.get(entry.entry_id, {}))
            cancel_auth_release.set()
            control = manager.control_lanes[entry.entry_id]["-200"]
            await asyncio.wait_for(asyncio.gather(control, return_exceptions=True), timeout=1)
            self.assertEqual(manager.cancel_epochs[entry.entry_id]["-200"], 1)
            runtime_release.set()
            turn = manager.turn_tasks["entry:-200"]
            await asyncio.wait_for(asyncio.gather(turn, return_exceptions=True), timeout=1)
            await asyncio.wait_for(manager.queues[entry.entry_id].join(), timeout=1)
            manager.send.assert_awaited_once_with("entry", "-200", "Остановлено.")
            cancel_auth_release.set()
        finally:
            runtime_release.set()
            cancel_auth_release.set()
            for reader in readers:
                reader.cancel()
            for task in tuple(manager.control_tasks.get(entry.entry_id, ())):
                if not task.done():
                    task.cancel()
            consumer.cancel()
            await asyncio.gather(
                *readers, *tuple(manager.control_tasks.get(entry.entry_id, ())),
                consumer, return_exceptions=True)

        manager.send.assert_awaited_once_with("entry", "-200", "Остановлено.")
        assistant.run.assert_awaited_once()

    async def test_departed_group_sender_cannot_cancel_another_members_turn(self):
        for member_status, is_member in (("left", False), ("restricted", False)):
            with self.subTest(member_status=member_status):
                manager = self.manager()
                entry = self.group_entry()
                self.bind_owner(manager, entry)
                self.bind_group(manager, entry)
                assistant = manager.hass.data["habab_home"]["assistant"]
                manager.queues[entry.entry_id] = asyncio.Queue(maxsize=2)
                manager.typing = AsyncMock(return_value=True)
                runtime_started = asyncio.Event()
                runtime_release = asyncio.Event()

                async def get_member(_entry_id, _group_id, user):
                    if user == 99:
                        return {"status": "administrator", "is_member": True}
                    if user == 52:
                        return {"status": member_status, "is_member": is_member}
                    return {"status": "creator", "is_member": True}

                async def get_group_session(owner, entry_id, chat_id):
                    assistant.telegram_sessions[f"{owner}:{entry_id}:{chat_id}"] = "shared-group-session"
                    return {"session_id": "shared-group-session"}

                async def run(*_args):
                    runtime_started.set()
                    await runtime_release.wait()
                    return {"messages": [{"role": "assistant", "text": "authorized answer"}]}

                manager.get_chat_member.side_effect = get_member
                assistant.telegram_session.side_effect = get_group_session
                assistant.run.side_effect = run
                consumer = asyncio.create_task(manager._consume(entry))
                readers = []
                try:
                    readers.append(await self.dispatch_worker_update(manager, entry, {
                        "chat_type": "supergroup", "chat_id": -200, "user_id": 20,
                        "message_id": 1, "text": "Start shared turn",
                    }))
                    await asyncio.wait_for(runtime_started.wait(), timeout=1)
                    readers.append(await self.dispatch_worker_update(manager, entry, {
                        "chat_type": "supergroup", "chat_id": -200, "user_id": 52,
                        "message_id": 2, "text": "/cancel",
                    }))
                    await asyncio.wait_for(asyncio.gather(
                        *tuple(manager.control_tasks[entry.entry_id]), return_exceptions=True),
                        timeout=1,
                    )
                    runtime_release.set()
                    await asyncio.wait_for(manager.queues[entry.entry_id].join(), timeout=1)
                finally:
                    runtime_release.set()
                    for reader in readers:
                        reader.cancel()
                    consumer.cancel()
                    await asyncio.gather(*readers, consumer, return_exceptions=True)

                self.assertNotIn("-200", manager.cancel_epochs.get(entry.entry_id, {}))
                manager.send.assert_awaited_once_with("entry", "-200", "authorized answer")

    async def test_pending_group_cancel_auth_check_is_released_after_failure_or_cancel(self):
        for outcome in ("failure", "cancel"):
            with self.subTest(outcome=outcome):
                manager = self.manager()
                entry = self.group_entry()
                self.bind_owner(manager, entry)
                self.bind_group(manager, entry)
                started = asyncio.Event()
                release = asyncio.Event()

                async def control(_entry, _msg):
                    started.set()
                    await release.wait()
                    if outcome == "failure":
                        raise RuntimeError("synthetic group control failure")

                manager._handle_control = control
                manager._schedule_control(entry, {
                    "chat_type": "supergroup", "chat_id": -200,
                    "user_id": 20, "message_id": 1, "text": "/cancel",
                })
                check = next(iter(manager.pending_group_cancels[entry.entry_id]["-200"]))
                waiter = asyncio.create_task(
                    manager._wait_for_group_cancel_checks(entry.entry_id, "-200", True))
                task = manager.control_lanes[entry.entry_id]["-200"]
                await asyncio.wait_for(started.wait(), timeout=1)
                if outcome == "cancel":
                    task.cancel()
                else:
                    release.set()
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=1)
                await asyncio.wait_for(waiter, timeout=1)

                self.assertTrue(check.done())
                self.assertNotIn("-200", manager.pending_group_cancels.get(entry.entry_id, {}))

    async def test_group_member_revocation_suppresses_inflight_shared_reply_without_forgetting_history(self):
        manager = self.manager()
        entry = self.group_entry()
        self.bind_owner(manager, entry)
        binding = self.bind_group(manager, entry)
        assistant = manager.hass.data["habab_home"]["assistant"]
        manager.typing = AsyncMock(return_value=True)
        membership_checks = 0
        run_started = asyncio.Event()

        async def get_member(_entry, _group, user):
            nonlocal membership_checks
            if user == 99:
                return {"status": "administrator", "is_member": True}
            membership_checks += 1
            if membership_checks <= 3:
                return {"status": "member", "is_member": True}
            return {"status": "left", "is_member": False}

        async def get_group_session(owner, entry_id, chat_id):
            assistant.telegram_sessions[f"{owner}:{entry_id}:{chat_id}"] = "shared-group-session"
            return {"session_id": "shared-group-session"}

        async def run(*_args):
            run_started.set()
            await asyncio.Event().wait()
            return {"messages": [{"role": "assistant", "text": "must not be sent"}]}

        manager.get_chat_member.side_effect = get_member
        assistant.telegram_session.side_effect = get_group_session
        assistant.run.side_effect = run
        import homeassistant.components.habab_telegram as telegram_component
        original_sleep = asyncio.sleep

        async def fast_sleep(_delay):
            await original_sleep(0)

        with patch.object(telegram_component.asyncio, "sleep", new=fast_sleep):
            turn = asyncio.create_task(manager._run_message(
                entry, "-200", 50, "member request", binding, 0, group_chat=True))
            manager.turn_tasks[manager._turn_key(entry.entry_id, "-200")] = turn
            await asyncio.wait_for(run_started.wait(), timeout=1)
            await asyncio.wait_for(asyncio.gather(turn, return_exceptions=True), timeout=1)

        self.assertGreaterEqual(membership_checks, 4)
        manager.send.assert_not_awaited()
        assistant.telegram_forget.assert_not_awaited()
        self.assertEqual(assistant.telegram_sessions[
            "alice:entry:-200"], "shared-group-session")

    async def test_setup_completion_from_member_dm_never_broadcasts_to_group(self):
        manager = self.manager()
        entry = self.group_entry()
        self.bind_owner(manager, entry)
        manager.entries["entry"].update(ready=True, username="testbot")
        manager.group_bindings["entry"] = {
            "chat_id": "-200", "title": "Home", "bot_id": 99, "owner_user_id": 20,
        }
        manager.get_chat_member.side_effect = lambda _entry, _group, user: (
            {"status": "administrator", "is_member": True} if user == 99 else
            {"status": "member", "is_member": True})
        manager.send.return_value = True
        origin = {"channel": "telegram", "owner": "alice", "entry_id": "entry",
                  "chat_id": "50", "bot_id": 99, "group_id": "-200", "user_id": 50}

        self.assertTrue(await manager.notify_setup_complete(origin, "Xiaomi Home"))
        manager.send.assert_awaited_once_with(
            "entry", "50", "Настройка Xiaomi Home завершена: интеграция загружена в Home Assistant.")
        self.assertNotEqual(manager.send.await_args.args[1], "-200")
        self.assertGreaterEqual(manager.get_chat_member.await_count, 4)

    async def test_setup_completion_rechecks_active_owner_exact_bot_and_current_private_binding(self):
        manager = self.manager()
        entry = SimpleNamespace(entry_id="entry", data={
            "owner_id": "alice", "bot_username": "testbot", "bot_id": 99,
        })
        manager.hass.config_entries = SimpleNamespace(async_entries=lambda _domain: [entry])
        manager.bindings = {"entry": {"chat_id": "10", "user_id": "20", "bot_id": 99}}
        manager.entries["entry"].update(ready=True, username="testbot")
        manager.send = AsyncMock(return_value=True)
        origin = {"channel": "telegram", "owner": "alice", "entry_id": "entry",
                  "chat_id": "10", "bot_id": 99}
        self.assertTrue(await manager.notify_setup_complete(origin, "fixture"))
        manager.send.assert_awaited_once_with(
            "entry", "10", "Настройка fixture завершена: интеграция загружена в Home Assistant.")
        manager.send.reset_mock()
        entry.data["bot_id"] = 100
        entry.data["bot_username"] = "replacementbot"
        manager.bindings["entry"]["bot_id"] = 100
        manager.entries["entry"]["username"] = "replacementbot"
        self.assertFalse(await manager.notify_setup_complete(origin, "fixture"))
        manager.send.assert_not_awaited()
        entry.data["bot_id"] = 99
        entry.data["bot_username"] = "testbot"
        manager.bindings["entry"]["bot_id"] = 99
        manager.entries["entry"]["username"] = "testbot"
        manager.bindings["entry"]["chat_id"] = "11"
        self.assertFalse(await manager.notify_setup_complete(origin, "fixture"))
        manager.bindings["entry"]["chat_id"] = "10"
        manager.send.return_value = False
        self.assertFalse(await manager.notify_setup_complete(origin, "fixture"))
        manager.send.reset_mock()
        manager.hass.auth.async_get_user.return_value = SimpleNamespace(is_active=False, is_admin=True)
        self.assertFalse(await manager.notify_setup_complete(
            {**origin, "chat_id": "11"}, "fixture"))
        manager.send.assert_not_awaited()

    async def test_identity_and_private_chat_are_enforced(self):
        manager = self.manager()
        entry = SimpleNamespace(entry_id="entry", data={"owner_id": "alice", "bot_username": "testbot", "bot_id": 99})
        await manager.handle_one(entry, {"chat_type": "group", "chat_id": 10, "user_id": 20, "message_id": 1, "text": "hello"})
        await manager.handle_one(entry, {"chat_type": "private", "chat_id": 11, "user_id": 20, "message_id": 2, "text": "hello"})
        manager.hass.auth.async_get_user.assert_awaited()
        manager.send.assert_awaited_once()
        self.assertIn("владельца", manager.send.await_args.args[2])
        assistant = manager.hass.data["habab_home"]["assistant"]
        assistant.telegram_session.assert_not_awaited()
        assistant.run.assert_not_awaited()
        self.assertEqual(manager.dedupe, set())
        self.assertEqual(manager.entries["entry"]["chat_id"], "10")

    async def test_persisted_replay_guard_and_commands(self):
        manager = self.manager()
        entry = SimpleNamespace(entry_id="entry", data={"owner_id": "alice"})
        message = {"chat_type": "private", "chat_id": 10, "user_id": 20, "message_id": 3, "text": "/help"}
        await manager.handle_one(entry, message)
        await manager.handle_one(entry, message)
        self.assertEqual(len(manager.dedupe), 1)
        manager.store.async_save.assert_awaited_once()
        manager.send.assert_awaited_once()
        self.assertIn("/new", manager.send.await_args.args[2])

    async def test_owner_admin_is_rechecked_before_commands(self):
        manager = self.manager()
        manager.hass.auth.async_get_user.return_value = SimpleNamespace(is_active=True, is_admin=False)
        entry = SimpleNamespace(entry_id="entry", data={"owner_id": "alice"})
        await manager.handle_one(entry, {"chat_type": "private", "chat_id": 10, "user_id": 20, "message_id": 4, "text": "/help"})
        manager.send.assert_awaited_once_with("entry", "10", "Доступ отозван.")

    async def test_unlink_forgets_only_bound_chat_after_worker_stops(self):
        manager = self.manager()
        entry = SimpleNamespace(entry_id="entry", data={"owner_id": "alice"})
        manager.hass.config_entries = SimpleNamespace(
            async_entries=lambda domain: [entry], async_remove=AsyncMock())
        manager.bindings = {
            "entry": {"chat_id": "10", "user_id": "20"},
            "other-entry": {"chat_id": "30", "user_id": "40"},
        }
        assistant = manager.hass.data["habab_home"]["assistant"]
        assistant.telegram_sessions = {
            "alice:entry:10": "entry-session",
            "alice:other-entry:30": "other-session",
            "bob:entry:10": "other-owner-session",
        }
        events = []

        async def close_entry(entry_id):
            events.append(("close", entry_id))

        async def forget(owner, entry_id, chat_id):
            events.append(("forget", owner, entry_id, chat_id))
            return assistant.telegram_sessions.pop(f"{owner}:{entry_id}:{chat_id}", None)

        manager.close_entry = AsyncMock(side_effect=close_entry)
        assistant.telegram_forget = AsyncMock(side_effect=forget)
        await manager.unlink("alice")

        self.assertEqual(events, [
            ("close", "entry"), ("forget", "alice", "entry", "10"),
        ])
        self.assertEqual(assistant.telegram_sessions, {
            "alice:other-entry:30": "other-session",
            "bob:entry:10": "other-owner-session",
        })
        manager.bindings_store.async_save.assert_awaited_once_with({
            "other-entry": {"chat_id": "30", "user_id": "40"},
        })
        manager.hass.config_entries.async_remove.assert_awaited_once_with("entry")

    async def test_home_assistant_load_order_reports_retry_instead_of_losing_turn_silently(self):
        manager = self.manager()
        manager.hass.data["habab_home"].pop("assistant")
        entry = self.group_entry()
        self.bind_owner(manager, entry)
        await manager._run_message(entry, "10", 20, "read integrations")
        manager.send.assert_awaited_once_with(
            "entry", "10", "Помощник запускается. Повторите запрос позже.")

    async def test_inactive_owner_cannot_create_or_complete_pairing(self):
        manager = self.manager()
        manager.hass.auth.async_get_user.return_value = SimpleNamespace(is_active=False, is_admin=True)
        entry = SimpleNamespace(entry_id="entry", data={"owner_id": "alice", "bot_username": "testbot", "bot_id": 99})
        manager.hass.config_entries = SimpleNamespace(async_entries=lambda domain: [entry])
        with self.assertRaisesRegex(ValueError, "^not_allowed$"):
            await manager.pair("alice")
        nonce = "single-use"
        manager.entries["entry"].update(chat_id=None, user_id=None)
        manager.pairing["entry"] = {
            "hash": hashlib.sha256(nonce.encode()).hexdigest(),
            "expires": 9999999999,
            "url": "https://t.me/testbot?start=single-use",
        }
        await manager.handle_one(entry, {
            "chat_type": "private", "chat_id": 10, "user_id": 20,
            "message_id": 11, "text": f"/start {nonce}",
        })
        self.assertNotIn("entry", manager.bindings)
        self.assertNotIn("entry", manager.pairing)

    async def test_send_waits_for_worker_ack(self):
        manager = self.manager()

        async def acknowledge(entry_id, payload):
            manager.pending_sends[payload["request_id"]][1].set_result(True)
            return True

        manager._write.side_effect = acknowledge
        delivered = await TelegramManager.send(manager, "entry", "10", "answer")
        self.assertTrue(delivered)
        self.assertEqual(manager.pending_sends, {})

    async def test_send_without_worker_ack_times_out_without_claiming_delivery(self):
        manager = self.manager()
        manager._write.return_value = True
        native_wait_for = asyncio.wait_for

        async def fast_wait_for(awaitable, timeout):
            return await native_wait_for(awaitable, 0.01)

        # Exercise the actual future timeout, without waiting 18 seconds.
        with patch(
            "homeassistant.components.habab_telegram.asyncio.wait_for",
            new=fast_wait_for,
        ):
            delivered = await TelegramManager.send(manager, "entry", "10", "answer")
        self.assertFalse(delivered)
        manager._write.assert_awaited_once()
        self.assertEqual(manager.pending_sends, {})

    async def test_send_pipe_failure_does_not_leave_an_unacknowledged_request(self):
        manager = self.manager()
        manager._write.side_effect = BrokenPipeError()
        self.assertFalse(await TelegramManager.send(manager, "entry", "10", "answer"))
        self.assertEqual(manager.pending_sends, {})

    async def test_pairing_again_revokes_old_chat_and_session(self):
        manager = self.manager()
        entry = SimpleNamespace(entry_id="entry", data={"owner_id": "alice", "bot_id": 7, "bot_username": "testbot"})
        manager.hass.config_entries = SimpleNamespace(async_entries=lambda domain: [entry])
        manager.bindings["entry"] = {"chat_id": "10", "user_id": "20", "bot_id": 7}
        manager.entries["entry"].update(chat_id="10", user_id="20")
        agent = manager.hass.data["habab_home"]["setup_agent"]
        agent.active = ("alice", "session-old")
        assistant = manager.hass.data["habab_home"]["assistant"]
        assistant.telegram_sessions = {"alice:entry:10": "session-old"}
        assistant.telegram_forget = AsyncMock(return_value="session-old")
        await manager.pair("alice")
        self.assertIsNone(manager.entries["entry"]["chat_id"])
        self.assertNotIn("entry", manager.bindings)
        assistant.telegram_forget.assert_awaited_once_with("alice", "entry", "10")

    async def test_configure_uses_trusted_owner_context_and_returns_pairable_status(self):
        manager = self.manager()
        entry = SimpleNamespace(entry_id="entry", data={
            "owner_id": "alice", "bot_id": 7, "bot_username": "testbot",
        })
        flow = SimpleNamespace(async_init=AsyncMock(return_value={"type": "create_entry"}),
                               async_abort=AsyncMock())
        manager.hass.config_entries = SimpleNamespace(
            async_entries=lambda domain: [entry], flow=flow,
        )
        await manager.configure("alice", "123456:abcdefghijklmnopqrstuvwxyz")
        flow.async_init.assert_awaited_once_with(
            "habab_telegram",
            context={"source": "user", "habab_owner_id": "alice"},
            data={"token": "123456:abcdefghijklmnopqrstuvwxyz", "proxy_url": ""},
        )
        status = await manager.status("alice")
        self.assertEqual(status["phase"], "pairing")
        self.assertIn("pairing_url", status)

    async def test_ready_worker_is_connected_and_can_be_paired_after_restart(self):
        manager = self.manager()
        entry = SimpleNamespace(entry_id="entry", data={"owner_id": "alice", "bot_username": "testbot", "bot_id": 99})
        manager.hass.config_entries = SimpleNamespace(async_entries=lambda domain: [entry])
        manager.entries["entry"]["ready"] = True
        manager.entries["entry"].update(chat_id="10", user_id="20")
        manager.bindings["entry"] = {"chat_id": "10", "user_id": "20"}
        self.assertEqual((await manager.status("alice"))["phase"], "connected")
        manager.bindings.clear()
        status = await manager.status("alice")
        self.assertEqual(status["phase"], "pairing")
        self.assertIn("pairing_url", status)

    async def test_full_reply_is_split_without_truncation_and_each_chunk_acked(self):
        manager = self.manager()
        payloads = []

        async def acknowledge(entry_id, payload):
            payloads.append(payload)
            self.assertLessEqual(len((json.dumps(payload, ensure_ascii=False) + "\n").encode()), 65536)
            manager.pending_sends[payload["request_id"]][1].set_result(True)
            return True

        manager._write.side_effect = acknowledge
        reply = "\n" * 40000
        self.assertTrue(await TelegramManager.send(manager, "entry", "10", reply))
        self.assertEqual(manager._write.await_count, len(payloads))
        self.assertGreater(len(payloads), 1)
        self.assertEqual("".join(payload["text"] for payload in payloads), reply)

    async def test_reader_routes_cancel_around_queued_run_and_marks_eof(self):
        started = asyncio.Event()

        class Agent:
            def __init__(self):
                self.telegram_sessions = {"alice:entry:10": "session"}
                self.active = None
                self.abort = AsyncMock(return_value=True)
                self.calls = []
                self.next_done = asyncio.Event()

            async def telegram_session(self, owner, entry_id, chat_id):
                return {"session_id": "session"}

            async def run(self, owner, channel, session_id, text, entry_id, chat_id):
                self.calls.append(text)
                self.active = (owner, session_id)
                if text == "do work":
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        await self.abort(owner, session_id)
                        raise
                self.next_done.set()
                return {"messages": [{"role": "assistant", "text": "next done"}]}

        class Stdout:
            def __init__(self, lines, control_gate):
                self.lines = list(lines)
                self.control_gate = control_gate

            async def readline(self):
                if self.lines and b"/cancel" in self.lines[0]:
                    await self.control_gate.wait()
                return self.lines.pop(0) if self.lines else b""

        class Process:
            def __init__(self, lines, control_gate, finish_gate):
                self.stdout = Stdout(lines, control_gate)
                self.returncode = None
                self.pid = 1
                self.finish_gate = finish_gate
                self.wait = AsyncMock(side_effect=self.finish)

            async def finish(self):
                await self.finish_gate.wait()
                self.returncode = 0
                return 0

        manager = self.manager()
        entry = self.group_entry()
        self.bind_owner(manager, entry)
        control_gate = asyncio.Event()
        finish_gate = asyncio.Event()
        manager.entries["entry"]["process"] = Process([
            b'{"type":"message","update_id":8,"chat_id":10,"user_id":20,"message_id":8,"chat_type":"private","text":"do work"}\n',
            b'{"type":"message","update_id":9,"chat_id":10,"user_id":20,"message_id":9,"chat_type":"private","text":"/cancel"}\n',
            b'{"type":"message","update_id":10,"chat_id":10,"user_id":20,"message_id":10,"chat_type":"private","text":"next work"}\n',
        ], control_gate, finish_gate)
        manager.queues["entry"] = asyncio.Queue(maxsize=5)
        agent = Agent()
        assistant = manager.hass.data["habab_home"]["assistant"]
        assistant.telegram_sessions = agent.telegram_sessions
        assistant.telegram_session = agent.telegram_session
        assistant.run = agent.run
        assistant.abort = agent.abort
        manager.typing = AsyncMock(return_value=True)
        consumer = asyncio.create_task(manager._consume(entry))
        reader = asyncio.create_task(manager._reader(entry, manager.entries["entry"]))
        await asyncio.wait_for(started.wait(), timeout=1)
        control_gate.set()
        await manager.queues["entry"].join()
        await asyncio.wait_for(agent.next_done.wait(), timeout=1)
        finish_gate.set()
        await reader
        tasks = manager.control_tasks.get("entry", set())
        await asyncio.gather(*list(tasks))
        self.assertTrue(agent.abort.awaited)
        self.assertEqual(agent.calls, ["do work", "next work"])
        self.assertEqual(len(manager.dedupe), 3)
        self.assertEqual(manager.store.async_save.await_count, 3)
        manager.send.assert_any_await("entry", "10", "Остановлено.")
        self.assertEqual(manager.entries["entry"]["error"], "worker_stopped")
        manager.entries["entry"]["process"].wait.assert_awaited_once()
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)

    async def test_full_ram_queue_retains_updates_in_durable_inbox_without_sending(self):
        manager = self.manager()
        entry = self.group_entry()
        manager.queues["entry"] = asyncio.Queue(maxsize=1)
        manager.queues["entry"].put_nowait({"already": "queued"})
        reader = await self.dispatch_worker_update(manager, entry, {
            "chat_id": 10, "user_id": 20, "message_id": 4,
            "chat_type": "private", "text": "hello",
        })
        try:
            self.assertEqual(manager.inbox.counts(entry)["queued"], 1)
            manager.send.assert_not_awaited()
            manager._write.assert_awaited_once_with("entry", {"type": "ack_update", "update_id": 4})
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    async def test_pairing_nonce_is_single_use_and_private_only(self):
        manager = self.manager()
        manager.entries["entry"]["chat_id"] = None
        manager.entries["entry"]["user_id"] = None
        manager.hass.data["habab_home"]["assistant"].telegram_forget = AsyncMock()
        entry = SimpleNamespace(entry_id="entry", data={"owner_id": "alice", "bot_id": 7, "bot_username": "testbot"})
        manager.hass.config_entries = SimpleNamespace(async_entries=lambda domain: [entry])
        result = await manager.pair("alice")
        nonce = result["pairing_url"].split("start=", 1)[1]
        await manager.handle_one(entry, {"chat_type": "group", "chat_id": 10, "user_id": 20, "message_id": 5, "text": f"/start {nonce}"})
        self.assertNotIn("entry", manager.bindings)
        await manager.handle_one(entry, {"chat_type": "private", "chat_id": 10, "user_id": 20, "message_id": 6, "text": "/start wrong"})
        self.assertNotIn("entry", manager.bindings)
        await manager.handle_one(entry, {"chat_type": "private", "chat_id": 10, "user_id": 20, "message_id": 7, "text": f"/start {nonce}"})
        self.assertEqual(manager.bindings["entry"], {"chat_id": "10", "user_id": "20", "bot_id": 7})
        self.assertNotIn("entry", manager.pairing)

    async def test_missing_start_keeps_pairing_pending_without_running_assistant(self):
        manager = self.manager()
        manager.entries["entry"].update(chat_id=None, user_id=None)
        entry = SimpleNamespace(entry_id="entry", data={
            "owner_id": "alice", "bot_id": 7, "bot_username": "testbot",
        })
        manager.hass.config_entries = SimpleNamespace(async_entries=lambda _domain: [entry])
        link = await manager.pair("alice")
        manager.send.reset_mock()
        for message_id, text in enumerate(("hello", "/start", "/start wrong"), 1):
            with self.subTest(text=text):
                await manager.handle_one(entry, {
                    "chat_type": "private", "chat_id": 10, "user_id": 20,
                    "message_id": message_id, "text": text,
                })
                status = await manager.status("alice")
                self.assertEqual(status["phase"], "pairing")
                self.assertEqual(status["pairing_url"], link["pairing_url"])
                self.assertIsNone(manager.entries["entry"]["chat_id"])
                self.assertNotIn("entry", manager.bindings)
        self.assertEqual(manager.send.await_count, 3)
        self.assertTrue(all(call.args == ("entry", 10, BOT_ONBOARDING_MESSAGE)
                            for call in manager.send.await_args_list))
        assistant = manager.hass.data["habab_home"]["assistant"]
        assistant.telegram_session.assert_not_awaited()
        assistant.run.assert_not_awaited()
        self.assertEqual(manager.dedupe, set())


class TokenValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_getme_does_not_return_token_or_raw_error(self):
        class Response:
            status = 200
            async def json(self, content_type=None):
                return {"ok": True, "result": {"id": 123, "username": "safe_bot"}}
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return None
        class Session:
            def get(self, *args, **kwargs): return Response()
        valid_token = "123456:abcdefghijklmnopqrstuvwxyz"
        self.assertEqual(await get_bot(Session(), valid_token), {"id": 123, "username": "safe_bot"})

        class BadSession:
            def get(self, *args, **kwargs):
                raise RuntimeError("123:private")
        with self.assertRaisesRegex(ValueError, "^cannot_validate$") as error:
            await get_bot(BadSession(), valid_token)
        self.assertNotIn(valid_token, str(error.exception))

    async def test_invalid_token_format_never_builds_a_bot_api_request(self):
        class Session:
            def get(self, *args, **kwargs):
                raise AssertionError("invalid token reached the network")

        with self.assertRaisesRegex(ValueError, "^invalid_token$"):
            await get_bot(Session(), "123:token/with/path")
