"""Family conversation reaches the bot only through literal addressing."""
import asyncio
import unittest
from unittest.mock import AsyncMock

from homeassistant.components.habab_telegram.group_invocation import group_request
import test_telegram_backend as _backend


class GroupSyntaxTests(unittest.TestCase):
    def test_selected_leading_aliases_and_exact_bot_mention(self):
        for text, expected in (
            ('Ко включи свет', 'включи свет'), ('ко, покажи устройства', 'покажи устройства'),
            ('КО: /help', '/help'), ('Эй дом, выключи свет', 'выключи свет'),
            ('эй ДОМ! покажи датчики', 'покажи датчики'),
            ('@testbot покажи устройства', 'покажи устройства'),
            ('Покажи @TeStBoT устройства', 'Покажи  устройства'),
            ('/help@testbot', '/help'), ('/cancel@TestBot', '/cancel'), ('/new@testbot', '/new'),
        ):
            with self.subTest(text=text):
                self.assertEqual(group_request(text, 'testbot'), expected)

    def test_unaddressed_other_bot_substrings_and_empty_requests_are_ignored(self):
        for text in ('привет семье', 'Коля включи свет', 'Кофе готов', 'Коala', 'Ко_тест',
                     'Эй дома хорошо', 'Ко', 'Эй дом!', '@testbot', '@testbot?',
                     '@otherbot покажи свет', '@testbot_extra покажи свет', 'x@testbot покажи свет',
                     'привет Ко включи свет', '/help', '/cancel', '/new', '/cancel@otherbot'):
            with self.subTest(text=text):
                self.assertIsNone(group_request(text, 'testbot'))


class GroupInvocationACLTests(unittest.IsolatedAsyncioTestCase):
    manager = _backend.TelegramManagerTests.manager
    group_entry = _backend.TelegramManagerTests.group_entry
    bind_owner = _backend.TelegramManagerTests.bind_owner
    bind_group = _backend.TelegramManagerTests.bind_group

    def bound_manager(self, member=True):
        manager = self.manager()
        entry = self.group_entry()
        self.bind_owner(manager, entry)
        self.bind_group(manager, entry)
        manager.typing = AsyncMock(return_value=True)
        manager.get_chat_member.side_effect = lambda _entry, _group, user: (
            {'status': 'administrator', 'is_member': True} if user == 99 else
            {'status': 'member', 'is_member': True} if member else
            {'status': 'left', 'is_member': False})
        assistant = manager.hass.data['habab_home']['assistant']
        async def session(owner, entry_id, chat_id):
            assistant.telegram_sessions[f'{owner}:{entry_id}:{chat_id}'] = 'shared'
            return {'session_id': 'shared'}
        assistant.telegram_session.side_effect = session
        assistant.run.return_value = {'messages': [{'text': 'Ответ'}]}
        return manager, entry, assistant

    async def dispatch(self, manager, entry, text, **extra):
        msg = {'chat_type': 'supergroup', 'chat_id': -200, 'user_id': 50,
               'message_id': 1, 'text': text, **extra}
        await manager.handle_one(entry, msg)
        await asyncio.gather(*manager.turn_tasks.values(), return_exceptions=True)
        return msg

    async def test_all_three_invocations_use_current_membership_and_common_assistant(self):
        for text in ('Ко покажи устройства', 'Эй дом, покажи устройства', '@testbot покажи устройства'):
            with self.subTest(text=text):
                manager, entry, assistant = self.bound_manager()
                message = await self.dispatch(manager, entry, text)
                assistant.run.assert_awaited_once_with('alice', 'telegram', 'shared', 'покажи устройства', 'entry', '-200')
                self.assertEqual(message['_habab_source_text'], text)
                manager.get_chat_member.assert_awaited()
                manager.send.assert_awaited_once_with('entry', '-200', 'Ответ')

    async def test_ordinary_family_conversation_and_bare_commands_do_no_ai_or_roster_work(self):
        for text in ('Что на ужин?', 'Коля покажи свет', '@otherbot покажи свет', '/help', '/new', '/cancel', 'Ко!'):
            with self.subTest(text=text):
                manager, entry, assistant = self.bound_manager()
                await self.dispatch(manager, entry, text)
                assistant.run.assert_not_awaited()
                assistant.telegram_session.assert_not_awaited()
                manager.get_chat_member.assert_not_awaited()
                manager.send.assert_not_awaited()
                self.assertFalse(manager._should_route_control(entry, manager.entries['entry'], {
                    'chat_type': 'supergroup', 'chat_id': -200, 'user_id': 50, 'message_id': 2, 'text': text}))

    async def test_departed_member_or_wrong_group_cannot_invoke_bot(self):
        for member, chat_id in ((False, -200), (True, -201)):
            with self.subTest(member=member, chat_id=chat_id):
                manager, entry, assistant = self.bound_manager(member=member)
                await self.dispatch(manager, entry, 'Эй дом, покажи устройства', chat_id=chat_id)
                assistant.run.assert_not_awaited()
                manager.send.assert_not_awaited()

    async def test_mention_matches_current_bot_username(self):
        manager, entry, assistant = self.bound_manager()
        manager.entries['entry']['username'] = 'currentbot'
        await self.dispatch(manager, entry, '@testbot покажи устройства')
        assistant.run.assert_not_awaited()
        await self.dispatch(manager, entry, '@currentbot покажи устройства', message_id=2)
        assistant.run.assert_awaited_once()

    async def test_addressed_help_works_and_does_not_enter_assistant(self):
        for text in ('Ко /help', 'Эй дом /help', '/help@testbot', '@testbot /help'):
            with self.subTest(text=text):
                manager, entry, assistant = self.bound_manager()
                await self.dispatch(manager, entry, text)
                assistant.run.assert_not_awaited()
                manager.send.assert_awaited_once()
                self.assertIn('/new', manager.send.call_args.args[2])
