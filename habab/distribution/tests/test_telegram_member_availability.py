"""Temporary roster errors deny access without erasing an existing conversation."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import test_telegram_backend as fixtures
from homeassistant.components import habab_telegram as component


class MemberAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, group=False):
        tools = fixtures.TelegramManagerTests()
        manager = tools.manager()
        entry = tools.group_entry()
        tools.bind_owner(manager, entry)
        tools.bind_group(manager, entry)
        chat = "-200" if group else "50"
        assistant = manager.hass.data["habab_home"]["assistant"]
        assistant.telegram_sessions[f"alice:entry:{chat}"] = "existing-session"
        assistant.telegram_session.return_value = {"session_id": "existing-session"}
        assistant.run.return_value = {"messages": [{"text": "private result"}]}
        manager.group_member_chats["entry"] = {"50": 50}
        manager.typing = AsyncMock(return_value=True)
        return manager, entry, assistant, chat

    def membership(self, manager, fail_at, unavailable_user=50, status=None):
        checks = 0

        def lookup(_entry, _group, user):
            nonlocal checks
            if user == unavailable_user:
                checks += 1
                if checks == fail_at:
                    return status
            return {"status": "administrator" if user == 99 else "member", "is_member": True}

        manager.get_chat_member.side_effect = lookup

    async def test_initial_dm_lookup_unavailable_preserves_session_and_next_request_recovers(self):
        for user in (99, 50):
            with self.subTest(user=user):
                manager, entry, assistant, _ = self.fixture()
                self.membership(manager, 1, user)
                await manager.handle_one(entry, {"chat_type": "private", "chat_id": 50,
                    "user_id": 50, "message_id": 100, "text": "/help"})
                assistant.telegram_forget.assert_not_awaited()
                assistant.telegram_session.assert_not_awaited()
                assistant.run.assert_not_awaited()
                self.assertIn("Не удалось проверить доступ", manager.send.await_args.args[2])
                self.assertNotIn("entry:50:100", manager.dedupe)
                await manager.handle_one(entry, {"chat_type": "private", "chat_id": 50,
                    "user_id": 50, "message_id": 101, "text": "/help"})
                self.assertIn("/new", manager.send.await_args.args[2])
                self.assertEqual(assistant.telegram_sessions["alice:entry:50"], "existing-session")
                assistant.telegram_forget.assert_not_awaited()

    async def test_initial_dm_confirmed_denial_still_revokes_access(self):
        manager, entry, assistant, _ = self.fixture()
        self.membership(manager, 1, status={"status": "left", "is_member": False})
        await manager.handle_one(entry, {"chat_type": "private", "chat_id": 50,
            "user_id": 50, "message_id": 100, "text": "/help"})
        assistant.telegram_forget.assert_awaited_once_with("alice", "entry", "50")
        assistant.telegram_session.assert_not_awaited()
        assistant.run.assert_not_awaited()

    async def test_claimed_request_rechecks_access_without_erasing_unknown_membership(self):
        for group in (False, True):
            with self.subTest(group=group):
                manager, entry, assistant, chat = self.fixture(group)
                self.membership(manager, 1)
                await manager.handle_one(entry, {"chat_type": "supergroup" if group else "private",
                    "chat_id": int(chat), "user_id": 50, "message_id": 100, "text": "/new",
                    "_habab_claimed": True, "_habab_group_message": group})
                assistant.telegram_forget.assert_not_awaited()
                assistant.telegram_reset.assert_not_awaited()
                assistant.run.assert_not_awaited()
                manager.send.assert_not_awaited()

    async def test_runtime_checks_fail_closed_and_distinguish_unavailable_from_denied(self):
        for group in (False, True):
            for user in (99, 50):
                for stage in range(1, 5):  # Admission, session load, typing, reply.
                    for denied in (False, True):
                        with self.subTest(group=group, user=user, stage=stage, denied=denied):
                            manager, entry, assistant, chat = self.fixture(group)
                            status = {"status": "member" if user == 99 else "left", "is_member": False} if denied else None
                            self.membership(manager, stage, user, status)
                            await manager._run_message(entry, chat, 50, "read-only fixture",
                                manager.group_bindings["entry"], 0, group_chat=group)
                            manager.send.assert_not_awaited()
                            self.assertEqual(assistant.run.await_count, int(stage == 4))
                            if denied and not group:
                                assistant.telegram_forget.assert_awaited_once_with("alice", "entry", chat)
                            else:
                                assistant.telegram_forget.assert_not_awaited()
                            self.assertEqual(assistant.telegram_sessions[f"alice:entry:{chat}"], "existing-session")

    async def test_monitor_cancels_uncertain_turn_without_revoking_history(self):
        for group in (False, True):
            for denied in (False, True):
                with self.subTest(group=group, denied=denied):
                    manager, entry, assistant, chat = self.fixture(group)
                    self.membership(manager, 1, status={"status": "left", "is_member": False} if denied else None)
                    turn = asyncio.create_task(asyncio.Event().wait())
                    sleep = asyncio.sleep

                    async def tick(_delay):
                        await sleep(0)

                    try:
                        with patch.object(component.asyncio, "sleep", new=tick):
                            await manager._monitor_group_member(entry, chat, 50,
                                manager.group_bindings["entry"], turn)
                        await asyncio.gather(turn, return_exceptions=True)
                        self.assertTrue(turn.cancelled())
                        if denied and not group:
                            assistant.telegram_forget.assert_awaited_once_with("alice", "entry", chat)
                        else:
                            assistant.telegram_forget.assert_not_awaited()
                            manager.send.assert_not_awaited()
                    finally:
                        turn.cancel()
                        await asyncio.gather(turn, return_exceptions=True)

    async def test_runtime_error_rechecks_membership_before_any_error_reply(self):
        for group in (False, True):
            for denied in (False, True):
                with self.subTest(group=group, denied=denied):
                    manager, entry, assistant, chat = self.fixture(group)
                    self.membership(manager, 4,
                        status={"status": "left", "is_member": False} if denied else None)
                    assistant.run.side_effect = RuntimeError("synthetic runtime error")
                    await manager._run_message(entry, chat, 50, "read-only fixture",
                        manager.group_bindings["entry"], 0, group_chat=group)
                    manager.send.assert_not_awaited()
                    if denied and not group:
                        assistant.telegram_forget.assert_awaited_once_with("alice", "entry", chat)
                    else:
                        assistant.telegram_forget.assert_not_awaited()

    async def test_unavailable_member_origin_and_setup_notification_are_not_authorized(self):
        for user in (99, 50):
            with self.subTest(user=user):
                manager, entry, assistant, _ = self.fixture()
                self.membership(manager, 1, user)
                self.assertIsNone(await manager.member_origin("alice", "entry", "50", "existing-session"))
                self.membership(manager, 1, user)
                origin = {"channel": "telegram", "owner": "alice", "entry_id": "entry",
                          "chat_id": "50", "bot_id": 99, "group_id": "-200", "user_id": 50}
                self.assertFalse(await manager.notify_setup_complete(origin, "fixture"))
                manager.send.assert_not_awaited()
                assistant.telegram_forget.assert_not_awaited()

    async def test_group_error_fallback_does_not_send_when_recheck_is_unavailable(self):
        manager, entry, assistant, _ = self.fixture(group=True)
        self.membership(manager, 2)
        manager.handle_one = AsyncMock(side_effect=RuntimeError("synthetic handler error"))
        await manager._consume_message(entry, {"chat_type": "supergroup", "chat_id": -200,
            "user_id": 50, "message_id": 100, "text": "read-only fixture"})
        manager.handle_one.assert_awaited_once()
        manager.send.assert_not_awaited()
        assistant.telegram_forget.assert_not_awaited()
