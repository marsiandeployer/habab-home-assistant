"""Crash boundaries for incoming text; no Bot API or model calls."""
import asyncio
import copy
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import CoreState, HomeAssistant
from homeassistant.components.habab_telegram.inbox import InboxStore, TelegramInbox
from homeassistant.components.habab_telegram.websocket import recover_inbox as recover_command
from homeassistant.exceptions import Unauthorized
import test_telegram_backend as fixtures


class SnapshotStore:
    def __init__(self):
        self.data = {}
        self.fail = False

    async def async_load(self):
        return copy.deepcopy(self.data)

    async def async_save(self, data):
        if self.fail:
            raise OSError("synthetic_storage_failure")
        self.data = copy.deepcopy(data)


def update(number=1, text="fixture", chat_id=10):
    return {"type": "message", "update_id": number, "message_id": number,
            "chat_id": chat_id, "user_id": 20, "chat_type": "private", "text": text}


class InboxTests(unittest.IsolatedAsyncioTestCase):
    def entry(self, bot=99, owner="alice"):
        return SimpleNamespace(entry_id="entry", data={"bot_id": bot, "owner_id": owner})

    async def test_native_store_roundtrip_preserves_queued_and_uncertain_states(self):
        with tempfile.TemporaryDirectory(prefix="telegram-inbox-native-") as root:
            hass = HomeAssistant(root)
            try:
                inbox = TelegramInbox(InboxStore(hass))
                await inbox.load()
                first = await inbox.accept(self.entry(), update())
                await inbox.accept(self.entry(), update(2))
                await inbox.begin(first)
                restored = TelegramInbox(InboxStore(hass))
                await restored.load()
                counts = restored.counts(self.entry())
                self.assertEqual({key: counts[key] for key in ("queued", "processing", "recovery_required")}, {
                    "queued": 1, "processing": 1, "recovery_required": True,
                })
                self.assertEqual(len(counts["recovery_id"]), 64)
                self.assertEqual(restored.queued(self.entry()), [])
                self.assertEqual(restored.records[first]["payload"], update())
                self.assertEqual(await restored.recover(
                    self.entry(), counts["recovery_id"], lambda: None), 2)
                recovered = TelegramInbox(InboxStore(hass))
                await recovered.load()
                self.assertEqual(recovered.counts(self.entry()), {
                    "queued": 0, "processing": 0, "recovery_required": False,
                })
            finally:
                await hass.async_stop(force=True)

    async def test_native_deferred_or_missing_write_is_not_a_commit_receipt(self):
        with tempfile.TemporaryDirectory(prefix="telegram-inbox-failure-") as root:
            hass = HomeAssistant(root)
            try:
                inbox = TelegramInbox(InboxStore(hass))
                # Simulate native Store swallowing a writer failure.
                with patch.object(inbox.store, "_async_write_data", new=AsyncMock()):
                    with self.assertRaises(OSError):
                        await inbox.accept(self.entry(), update())
                self.assertEqual(inbox.records, {})
                hass.set_state(CoreState.stopping)
                with self.assertRaises(OSError):
                    await inbox.accept(self.entry(), update())
                self.assertEqual(inbox.records, {})
            finally:
                hass.set_state(CoreState.not_running)
                await hass.async_stop(force=True)

    async def test_committed_before_ack_restart_and_completed_redelivery_are_deduplicated(self):
        store = SnapshotStore()
        inbox = TelegramInbox(store)
        key = await inbox.accept(self.entry(), update())
        restored = TelegramInbox(store)
        await restored.load()
        self.assertEqual(await restored.accept(self.entry(), update()), key)
        self.assertEqual(len(restored.queued(self.entry())), 1)
        self.assertTrue(await restored.begin(key))
        await restored.finish(key, update())
        again = TelegramInbox(store)
        await again.load()
        self.assertEqual(await again.accept(self.entry(), update()), key)
        self.assertEqual(again.queued(self.entry()), [])
        self.assertNotIn("payload", again.records[key])

    async def test_storage_failure_does_not_publish_receipt_and_changed_update_is_rejected(self):
        store = SnapshotStore()
        inbox = TelegramInbox(store)
        store.fail = True
        with self.assertRaises(OSError):
            await inbox.accept(self.entry(), update())
        self.assertEqual(inbox.records, {})
        store.fail = False
        await inbox.accept(self.entry(), update())
        with self.assertRaises(ValueError):
            await inbox.accept(self.entry(), update(text="changed"))

    async def test_full_inbox_applies_backpressure_without_discarding_existing_inputs(self):
        inbox = TelegramInbox(SnapshotStore())
        inbox.MAX_PENDING = 1
        await inbox.accept(self.entry(), update())
        with self.assertRaisesRegex(ValueError, "telegram_inbox_full"):
            await inbox.accept(self.entry(), update(2))
        self.assertEqual([payload for _, payload in inbox.queued(self.entry())], [update()])

    async def test_owner_cancel_does_not_revive_older_queued_messages_after_restart(self):
        store = SnapshotStore()
        inbox = TelegramInbox(store)
        await inbox.accept(self.entry(), update())
        cancel = update(2, "/cancel")
        key = await inbox.accept(self.entry(), cancel)
        await inbox.begin(key)
        await inbox.finish(key, {**cancel, "_habab_claimed": True})
        restored = TelegramInbox(store)
        await restored.load()
        self.assertEqual(restored.queued(self.entry()), [])
        self.assertFalse(restored.counts(self.entry())["recovery_required"])
        # A replacement bot never receives its predecessor's stored commands.
        await restored.accept(self.entry(), update(4))
        self.assertEqual(len(restored.queued(self.entry())), 1)
        self.assertEqual(restored.queued(self.entry(bot=100)), [])
        self.assertEqual(restored.queued(self.entry(owner="bob")), [])

    async def test_queued_restart_replays_once_and_tombstone_blocks_redelivery(self):
        fixture = fixtures.TelegramManagerTests()
        manager = fixture.manager()
        entry = fixture.group_entry()
        store = SnapshotStore()
        before = TelegramInbox(store)
        key = await before.accept(entry, update())
        manager.inbox = TelegramInbox(store)
        await manager.inbox.load()
        manager.queues[entry.entry_id] = asyncio.Queue(maxsize=5)
        manager._consume_message = AsyncMock()
        manager._pump_inbox(entry)
        consumer = asyncio.create_task(manager._consume(entry))
        try:
            await asyncio.wait_for(manager.queues[entry.entry_id].join(), 1)
            self.assertEqual(manager.inbox.records[key]["phase"], "done")
            manager._consume_message.assert_awaited_once()
            await manager.inbox.accept(entry, update())
            manager._pump_inbox(entry)
            self.assertTrue(manager.queues[entry.entry_id].empty())
            manager._consume_message.assert_awaited_once()
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    async def test_reader_storage_error_never_acknowledges_or_dispatches(self):
        fixture = fixtures.TelegramManagerTests()
        manager = fixture.manager()
        entry = fixture.group_entry()
        store = SnapshotStore()
        store.fail = True
        manager.inbox = TelegramInbox(store)
        manager.queues[entry.entry_id] = asyncio.Queue(maxsize=5)
        state = manager.entries[entry.entry_id]
        lines = asyncio.Queue()
        reached = asyncio.Event()

        class Stdout:
            async def readline(self):
                if lines.empty():
                    reached.set()
                return await lines.get()

        state["process"] = SimpleNamespace(stdout=Stdout())
        lines.put_nowait((json.dumps(update()) + "\n").encode())
        reader = asyncio.create_task(manager._reader(entry, state))
        waiting = asyncio.create_task(reached.wait())
        try:
            await asyncio.wait_for(asyncio.wait((reader, waiting), return_when=asyncio.FIRST_COMPLETED), 1)
            self.assertFalse(reader.done())
            manager._write.assert_not_awaited()
            self.assertTrue(manager.queues[entry.entry_id].empty())
            recovery = manager.inbox.counts(entry, manager._waiting_payload(entry))
            self.assertTrue(recovery["recovery_required"])
            self.assertEqual(recovery["queued"], 1)
            store.fail = False
            fixture.bind_owner(manager, entry)
            await manager.recover_inbox("alice", recovery["recovery_id"])
            manager._write.assert_awaited_once_with("entry", {"type": "ack_update", "update_id": 1})
            self.assertFalse(reader.done())
            self.assertTrue(manager.queues[entry.entry_id].empty())
            manager.send.assert_not_awaited()
        finally:
            reader.cancel()
            waiting.cancel()
            await asyncio.gather(reader, waiting, return_exceptions=True)

    async def test_reader_ack_waits_for_commit_and_cancelled_consumer_retains_input(self):
        fixture = fixtures.TelegramManagerTests()
        manager = fixture.manager()
        entry = fixture.group_entry()
        fixture.bind_owner(manager, entry)
        store = SnapshotStore()
        manager.inbox = TelegramInbox(store)
        entered, commit = asyncio.Event(), asyncio.Event()
        original_save = store.async_save

        async def delayed_save(data):
            entered.set()
            await commit.wait()
            await original_save(data)

        store.async_save = delayed_save
        dispatch = asyncio.create_task(fixture.dispatch_worker_update(manager, entry, update()))
        reader = None
        consumer = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            manager._write.assert_not_awaited()
            commit.set()
            reader = await asyncio.wait_for(dispatch, 1)
            manager._write.assert_awaited_once_with("entry", {"type": "ack_update", "update_id": 1})
            running = asyncio.Event()

            async def handling(*_args):
                running.set()
                await asyncio.Event().wait()

            manager._consume_message = AsyncMock(side_effect=handling)
            consumer = asyncio.create_task(manager._consume(entry))
            await asyncio.wait_for(running.wait(), 1)
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            restored = TelegramInbox(store)
            await restored.load()
            self.assertEqual(restored.counts(entry)["processing"], 1)
            self.assertTrue(restored.counts(entry)["recovery_required"])
            self.assertEqual(next(iter(restored.records.values()))["payload"]["text"], "fixture")
        finally:
            commit.set()
            dispatch.cancel()
            for task in (reader, consumer):
                if task:
                    task.cancel()
            await asyncio.gather(dispatch, *(task for task in (reader, consumer) if task), return_exceptions=True)

    async def recovery_manager(self):
        fixture = fixtures.TelegramManagerTests()
        manager = fixture.manager()
        entry = fixture.group_entry()
        fixture.bind_owner(manager, entry)
        store = SnapshotStore()
        manager.inbox = TelegramInbox(store)
        key = await manager.inbox.accept(entry, update())
        await manager.inbox.begin(key)
        await manager.inbox.accept(entry, update(2))
        await manager.inbox.load()  # Simulated restart with an ambiguous action.
        manager.queues[entry.entry_id] = asyncio.Queue(maxsize=5)
        return manager, entry, store

    async def test_owner_recovery_discards_snapshot_without_executing_and_resumes_new_inputs(self):
        manager, entry, store = await self.recovery_manager()
        waiting = update(3)
        manager.waiting_updates[entry.entry_id] = {
            "scope": manager.inbox.scope(entry), "payload": waiting,
        }
        state = manager.inbox.counts(entry, waiting)
        result = await manager.recover_inbox("alice", state["recovery_id"])
        self.assertEqual(result, {"recovered": True, "discarded": 3})
        self.assertFalse(manager.inbox.counts(entry)["recovery_required"])
        self.assertTrue(manager.queues[entry.entry_id].empty())
        manager._write.assert_awaited_once_with("entry", {"type": "ack_update", "update_id": 3})
        manager.send.assert_not_awaited()
        manager.hass.data["habab_home"]["assistant"].run.assert_not_awaited()
        restarted = TelegramInbox(store)
        await restarted.load()
        self.assertEqual(restarted.queued(entry), [])
        self.assertTrue(all("payload" not in record for record in restarted.records.values()))
        # A fresh request is admitted; discarded requests never reappear.
        await manager._receive_update(entry, update(4))
        self.assertEqual(manager.queues[entry.entry_id].get_nowait()["update_id"], 4)

    async def test_recovery_rejects_foreign_inactive_owner_stale_snapshot_and_failed_commit(self):
        manager, entry, store = await self.recovery_manager()
        state = manager.inbox.counts(entry)
        with self.assertRaisesRegex(ValueError, "not_configured"):
            await manager.recover_inbox("bob", state["recovery_id"])
        manager.hass.auth.async_get_user.return_value = SimpleNamespace(is_active=False, is_admin=True)
        with self.assertRaisesRegex(ValueError, "not_allowed"):
            await manager.recover_inbox("alice", state["recovery_id"])
        manager.hass.auth.async_get_user.return_value = SimpleNamespace(is_active=True, is_admin=False)
        with self.assertRaisesRegex(ValueError, "not_allowed"):
            await manager.recover_inbox("alice", state["recovery_id"])
        manager.hass.auth.async_get_user.return_value = SimpleNamespace(is_active=True, is_admin=True)
        active = asyncio.create_task(asyncio.Event().wait())
        manager.turn_tasks["entry:10"] = active
        try:
            with self.assertRaisesRegex(ValueError, "telegram_recovery_unavailable"):
                await manager.recover_inbox("alice", state["recovery_id"])
        finally:
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        manager.turn_tasks.clear()
        await manager.inbox.accept(entry, update(3))
        with self.assertRaisesRegex(ValueError, "telegram_recovery_stale"):
            await manager.recover_inbox("alice", state["recovery_id"])
        manager.waiting_updates[entry.entry_id] = {"scope": manager.inbox.scope(entry), "payload": update(4)}
        state = manager.inbox.counts(entry, update(4))
        manager.waiting_updates[entry.entry_id]["payload"] = update(5)
        with self.assertRaisesRegex(ValueError, "telegram_recovery_stale"):
            await manager.recover_inbox("alice", state["recovery_id"])
        state = manager.inbox.counts(entry, update(5))
        store.fail = True
        with self.assertRaises(OSError):
            await manager.recover_inbox("alice", state["recovery_id"])
        self.assertTrue(manager.inbox.counts(entry)["recovery_required"])
        manager._write.assert_not_awaited()
        manager.send.assert_not_awaited()

    async def test_recovery_cannot_discard_a_normal_running_turn(self):
        inbox = TelegramInbox(SnapshotStore())
        key = await inbox.accept(self.entry(), update())
        await inbox.begin(key)
        with self.assertRaisesRegex(ValueError, "telegram_recovery_unavailable"):
            await inbox.recover(self.entry(), "a" * 64, lambda: None)
        self.assertEqual(inbox.records[key]["phase"], "processing")

    async def test_max_pending_reader_stays_alive_and_ack_resumes_when_space_is_freed(self):
        fixture = fixtures.TelegramManagerTests()
        manager = fixture.manager()
        entry = fixture.group_entry()
        manager.inbox = TelegramInbox(SnapshotStore())
        manager.queues[entry.entry_id] = asyncio.Queue(maxsize=5)
        keys = []
        for number in range(1, manager.inbox.MAX_PENDING + 1):
            keys.append(await manager.inbox.accept(entry, update(number)))
        await manager.inbox.begin(keys[0])
        waiting = update(257)
        reached = asyncio.Event()
        future = asyncio.get_running_loop().create_future()
        manager.pending_sends["fixture-send"] = (entry.entry_id, future)

        class Stdout:
            def __init__(self):
                self.lines = [(json.dumps(waiting) + "\n").encode(),
                              b'{"type":"sent","request_id":"fixture-send"}\n']

            async def readline(self):
                if self.lines:
                    return self.lines.pop(0)
                reached.set()
                await asyncio.Event().wait()

        state = manager.entries[entry.entry_id]
        state["process"] = SimpleNamespace(stdout=Stdout())
        reader = asyncio.create_task(manager._reader(entry, state))
        waiting_for_reader = asyncio.create_task(reached.wait())
        try:
            await asyncio.wait_for(asyncio.wait(
                (reader, waiting_for_reader), return_when=asyncio.FIRST_COMPLETED), 1)
            self.assertFalse(reader.done())
            await asyncio.wait_for(waiting_for_reader, 1)
            self.assertTrue(future.result())  # Send ACKs are still consumed while inbox is full.
            manager._write.assert_not_awaited()
            self.assertEqual(manager._waiting_payload(entry), waiting)
            await manager._finish_incoming(entry, {**update(), "_habab_inbox_key": keys[0]})
            manager._write.assert_awaited_once_with("entry", {"type": "ack_update", "update_id": 257})
            self.assertIsNone(manager._waiting_payload(entry))
            record = manager.inbox.records[f"{manager.inbox.scope(entry)}:257"]
            self.assertEqual(record["payload"], waiting)
            self.assertFalse(reader.done())
            manager.send.assert_not_awaited()
        finally:
            reader.cancel()
            waiting_for_reader.cancel()
            await asyncio.gather(reader, waiting_for_reader, return_exceptions=True)

    async def test_native_recovery_websocket_requires_admin_and_uses_authenticated_owner(self):
        manager = SimpleNamespace(recover_inbox=AsyncMock(return_value={"recovered": True}))
        tasks = []

        def schedule(coroutine, *_args, **_kwargs):
            task = asyncio.create_task(coroutine)
            tasks.append(task)
            return task

        hass = SimpleNamespace(data={"habab_telegram": manager}, async_create_background_task=schedule)
        connection = SimpleNamespace(user=SimpleNamespace(id="alice", is_admin=False),
                                     send_result=Mock(), send_error=Mock())
        message = {"id": 1, "type": "habab_telegram/recover_inbox", "recovery_id": "a" * 64}
        with self.assertRaises(Unauthorized):
            recover_command(hass, connection, message)
        manager.recover_inbox.assert_not_awaited()
        connection.user.is_admin = True
        recover_command(hass, connection, message)
        await asyncio.gather(*tasks)
        manager.recover_inbox.assert_awaited_once_with("alice", "a" * 64)
        connection.send_result.assert_called_once_with(1, {"recovered": True})

    async def test_cancelled_ack_drain_does_not_reject_next_worker_update(self):
        fixture = fixtures.TelegramManagerTests()
        manager = fixture.manager()
        entry = fixture.group_entry()
        manager.inbox = TelegramInbox(SnapshotStore())
        manager.queues[entry.entry_id] = asyncio.Queue(maxsize=5)
        manager.waiting_updates[entry.entry_id] = {
            "scope": manager.inbox.scope(entry), "payload": update(),
        }
        ack_written = asyncio.Event()

        async def drain(*_args):
            ack_written.set()
            await asyncio.Event().wait()

        manager._write = AsyncMock(side_effect=drain)
        retry = asyncio.create_task(manager._retry_waiting_update(entry))
        try:
            await asyncio.wait_for(ack_written.wait(), 1)
        finally:
            retry.cancel()
            await asyncio.gather(retry, return_exceptions=True)
        self.assertEqual(manager.inbox.counts(entry)["queued"], 1)
        self.assertEqual(manager.inbox.counts(entry, update())["queued"], 1)
        manager._write = AsyncMock(return_value=True)
        self.assertTrue(await manager._receive_update(entry, update(2)))
        self.assertIsNone(manager._waiting_payload(entry))

    async def test_native_storage_failure_keeps_consumer_alive_without_repeating_actions(self):
        for phase in ("begin", "finish"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory(prefix="telegram-consumer-io-") as root:
                hass = HomeAssistant(root)
                consumer = None
                try:
                    fixture = fixtures.TelegramManagerTests()
                    manager = fixture.manager()
                    entry = fixture.group_entry()
                    fixture.bind_owner(manager, entry)
                    store = InboxStore(hass)
                    manager.inbox = TelegramInbox(store)
                    manager.queues[entry.entry_id] = asyncio.Queue(maxsize=5)
                    key = await manager.inbox.accept(entry, update())
                    manager._pump_inbox(entry)
                    actions = []
                    manager._consume_message = AsyncMock(side_effect=lambda _entry, msg: actions.append(msg["update_id"]))
                    write = store._async_write_data

                    async def interrupted_write(data):
                        requested = data["data"].get(key, {}).get("phase")
                        if requested == ("processing" if phase == "begin" else "done"):
                            return  # Core returned without writing; readback must detect it.
                        await write(data)

                    with patch.object(store, "_async_write_data", side_effect=interrupted_write):
                        consumer = asyncio.create_task(manager._consume(entry))
                        await asyncio.wait_for(manager.queues[entry.entry_id].join(), 1)
                        self.assertFalse(consumer.done())
                        state = manager.inbox.counts(entry)
                        self.assertTrue(state["recovery_required"])
                        self.assertEqual(actions, [] if phase == "begin" else [1])
                        self.assertEqual(manager.inbox.records[key]["payload"], update())
                        await asyncio.sleep(0)
                        self.assertTrue(manager.queues[entry.entry_id].empty())
                    await manager.recover_inbox("alice", state["recovery_id"])
                    await manager._receive_update(entry, update(2))
                    await asyncio.wait_for(manager.queues[entry.entry_id].join(), 1)
                    self.assertEqual(actions, [2] if phase == "begin" else [1, 2])
                    self.assertFalse(consumer.done())
                finally:
                    if consumer:
                        consumer.cancel()
                        await asyncio.gather(consumer, return_exceptions=True)
                    await hass.async_stop(force=True)

    async def test_control_storage_failure_requires_recovery_without_automatic_retry(self):
        for phase in ("begin", "finish"):
            with self.subTest(phase=phase):
                fixture = fixtures.TelegramManagerTests()
                manager = fixture.manager()
                entry = fixture.group_entry()
                store = SnapshotStore()
                manager.inbox = TelegramInbox(store)
                key = await manager.inbox.accept(entry, update(text="/cancel"))
                save = store.async_save

                async def interrupted_write(data):
                    if data[key]["phase"] == ("processing" if phase == "begin" else "done"):
                        raise OSError("synthetic_control_storage_error")
                    await save(data)

                store.async_save = interrupted_write
                manager.handle_one = AsyncMock()
                await manager._handle_control(entry, {**update(text="/cancel"), "_habab_inbox_key": key})
                self.assertEqual(manager.handle_one.await_count, 0 if phase == "begin" else 1)
                self.assertTrue(manager.inbox.counts(entry)["recovery_required"])
                self.assertEqual(manager.inbox.queued(entry), [])
