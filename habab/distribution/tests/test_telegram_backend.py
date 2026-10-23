"""Security and session boundaries for the native Telegram bridge."""
import asyncio
import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.components.habab_home.setup_agent import AgentError, SetupAgent
from homeassistant.components.habab_telegram import TelegramManager
from homeassistant.components.habab_telegram.config_flow import get_bot


class SetupAgentTelegramTests(unittest.IsolatedAsyncioTestCase):
    def test_web_history_is_unchanged_and_telegram_mapping_is_separate(self):
        agent = object.__new__(SetupAgent)
        agent.owners = {"alice": "web-session"}
        agent.telegram_sessions = {"alice:bot-a:chat-1": "tg-session"}
        self.assertTrue(agent._owns_session("alice", "web-session"))
        self.assertTrue(agent._owns_session("alice", "tg-session"))
        self.assertFalse(agent._owns_session("bob", "tg-session"))
        self.assertFalse(agent._owns_session("alice", "someone-elses-session"))

    async def test_cancel_requires_owner_and_mapped_session(self):
        agent = object.__new__(SetupAgent)
        agent.owners = {"alice": "web-session"}
        agent.telegram_sessions = {"alice:bot-a:chat-1": "tg-session"}
        agent.active = ("alice", "tg-session")
        agent.request = AsyncMock()
        with self.assertRaises(AgentError):
            await agent.abort("bob", "tg-session")
        await agent.abort("alice", "web-session")
        agent.request.assert_not_awaited()


class TelegramManagerTests(unittest.IsolatedAsyncioTestCase):
    def manager(self):
        manager = object.__new__(TelegramManager)
        manager.hass = SimpleNamespace(
            auth=SimpleNamespace(async_get_user=AsyncMock(return_value=SimpleNamespace(is_active=True, is_admin=True))),
            data={"habab_home": {"setup_agent": SimpleNamespace(
                telegram_sessions={}, active=None, telegram_forget=AsyncMock(),
            )}},
            async_create_task=asyncio.create_task,
        )
        manager.entries = {"entry": {"chat_id": "10", "user_id": "20", "username": "testbot"}}
        manager.pairing = {}
        manager.pair_locks = {}
        manager.bindings = {}
        manager.bindings_store = SimpleNamespace(async_save=AsyncMock())
        manager.dedupe = set()
        manager.dedupe_order = []
        manager.store = SimpleNamespace(async_save=AsyncMock())
        manager.send = AsyncMock()
        manager._write = AsyncMock()
        manager.queues = {}
        manager.tasks = {}
        manager.turn_tasks = {}
        manager.control_tasks = {}
        manager.pending_sends = {}
        return manager

    async def test_identity_and_private_chat_are_enforced(self):
        manager = self.manager()
        entry = SimpleNamespace(entry_id="entry", data={"owner_id": "alice", "bot_username": "testbot"})
        await manager.handle_one(entry, {"chat_type": "group", "chat_id": 10, "user_id": 20, "message_id": 1, "text": "hello"})
        await manager.handle_one(entry, {"chat_type": "private", "chat_id": 11, "user_id": 20, "message_id": 2, "text": "hello"})
        manager.hass.auth.async_get_user.assert_not_awaited()
        manager.send.assert_not_awaited()

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

    async def test_inactive_owner_cannot_create_or_complete_pairing(self):
        manager = self.manager()
        manager.hass.auth.async_get_user.return_value = SimpleNamespace(is_active=False, is_admin=True)
        entry = SimpleNamespace(entry_id="entry", data={"owner_id": "alice", "bot_username": "testbot"})
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

    async def test_pairing_again_revokes_old_chat_and_session(self):
        manager = self.manager()
        entry = SimpleNamespace(entry_id="entry", data={"owner_id": "alice", "bot_id": 7, "bot_username": "testbot"})
        manager.hass.config_entries = SimpleNamespace(async_entries=lambda domain: [entry])
        manager.bindings["entry"] = {"chat_id": "10", "user_id": "20", "bot_id": 7}
        manager.entries["entry"].update(chat_id="10", user_id="20")
        agent = manager.hass.data["habab_home"]["setup_agent"]
        agent.active = ("alice", "session-old")
        agent.telegram_sessions = {"alice:entry:10": "session-old"}
        agent.abort = AsyncMock(return_value=True)
        agent.telegram_forget = AsyncMock(return_value="session-old")
        await manager.pair("alice")
        self.assertIsNone(manager.entries["entry"]["chat_id"])
        self.assertNotIn("entry", manager.bindings)
        agent.abort.assert_awaited_once_with("alice", "session-old")
        agent.telegram_forget.assert_awaited_once_with("alice", "entry", "10")

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
            data={"token": "123456:abcdefghijklmnopqrstuvwxyz"},
        )
        status = await manager.status("alice")
        self.assertEqual(status["phase"], "pairing")
        self.assertIn("pairing_url", status)

    async def test_ready_worker_is_connected_and_can_be_paired_after_restart(self):
        manager = self.manager()
        entry = SimpleNamespace(entry_id="entry", data={"owner_id": "alice", "bot_username": "testbot"})
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

            async def run(self, owner, session_id, text):
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
            def __init__(self, lines, control_gate):
                self.stdout = Stdout(lines, control_gate)
                self.returncode = None
                self.pid = 1
                self.wait = AsyncMock(side_effect=self.finish)

            async def finish(self):
                self.returncode = 0
                return 0

        manager = self.manager()
        entry = SimpleNamespace(entry_id="entry", data={"owner_id": "alice"})
        control_gate = asyncio.Event()
        manager.entries["entry"]["process"] = Process([
            b'{"type":"message","chat_id":10,"user_id":20,"message_id":8,"chat_type":"private","text":"do work"}\n',
            b'{"type":"message","chat_id":10,"user_id":20,"message_id":9,"chat_type":"private","text":"/cancel"}\n',
            b'{"type":"message","chat_id":10,"user_id":20,"message_id":10,"chat_type":"private","text":"next work"}\n',
        ], control_gate)
        manager.queues["entry"] = asyncio.Queue(maxsize=5)
        agent = Agent()
        manager.hass.data["habab_home"]["setup_agent"] = agent
        manager.typing = AsyncMock(return_value=True)
        consumer = asyncio.create_task(manager._consume(entry))
        reader = asyncio.create_task(manager._reader(entry, manager.entries["entry"]))
        await asyncio.wait_for(started.wait(), timeout=1)
        control_gate.set()
        await reader
        tasks = manager.control_tasks["entry"]
        await asyncio.gather(*list(tasks))
        await manager.queues["entry"].join()
        await asyncio.wait_for(agent.next_done.wait(), timeout=1)
        self.assertTrue(agent.abort.awaited)
        self.assertEqual(agent.calls, ["do work", "next work"])
        self.assertEqual(len(manager.dedupe), 3)
        self.assertEqual(manager.store.async_save.await_count, 3)
        manager.send.assert_any_await("entry", "10", "Остановлено.")
        self.assertEqual(manager.entries["entry"]["error"], "worker_stopped")
        manager.entries["entry"]["process"].wait.assert_awaited_once()
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)

    async def test_queue_overflow_ack_is_private_bound_and_admin_only(self):
        class Stdout:
            def __init__(self, lines):
                self.lines = list(lines)

            async def readline(self):
                return self.lines.pop(0) if self.lines else b""

        class Process:
            def __init__(self, lines):
                self.stdout = Stdout(lines)
                self.returncode = 0
                self.wait = AsyncMock(return_value=0)

        entry = SimpleNamespace(entry_id="entry", data={"owner_id": "alice"})
        manager = self.manager()
        manager.queues["entry"] = asyncio.Queue(maxsize=1)
        manager.queues["entry"].put_nowait({"already": "queued"})
        manager.entries["entry"]["process"] = Process([
            b'{"type":"message","chat_id":10,"user_id":20,"message_id":1,"chat_type":"group","text":"hello"}\n',
            b'{"type":"message","chat_id":99,"user_id":20,"message_id":2,"chat_type":"private","text":"hello"}\n',
            b'{"type":"message","chat_id":10,"user_id":21,"message_id":3,"chat_type":"private","text":"hello"}\n',
            b'{"type":"message","chat_id":10,"user_id":20,"message_id":4,"chat_type":"private","text":"hello"}\n',
        ])
        await manager._reader(entry, manager.entries["entry"])
        manager.send.assert_awaited_once_with("entry", 10, "Очередь занята. Попробуйте позже.")
        self.assertEqual(manager.dedupe, {"entry:10:4"})

        inactive = self.manager()
        inactive.hass.auth.async_get_user.return_value = SimpleNamespace(is_active=False, is_admin=True)
        inactive.queues["entry"] = asyncio.Queue(maxsize=1)
        inactive.queues["entry"].put_nowait({"already": "queued"})
        inactive.entries["entry"]["process"] = Process([
            b'{"type":"message","chat_id":10,"user_id":20,"message_id":5,"chat_type":"private","text":"hello"}\n',
        ])
        await inactive._reader(entry, inactive.entries["entry"])
        inactive.send.assert_not_awaited()
        self.assertIsNone(inactive.entries["entry"]["chat_id"])

    async def test_pairing_nonce_is_single_use_and_private_only(self):
        manager = self.manager()
        manager.entries["entry"]["chat_id"] = None
        manager.entries["entry"]["user_id"] = None
        manager.hass.data["habab_home"]["setup_agent"].telegram_forget = AsyncMock()
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
