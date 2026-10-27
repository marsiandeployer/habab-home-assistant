"""Cold-start maintenance never consumes updates or marks notifications delivered."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import HomeAssistant
from homeassistant.components.habab_telegram import TelegramManager
from homeassistant.components.habab_telegram.config_flow import HababTelegramConfigFlow
from homeassistant.components.habab_telegram.maintenance import MAINTENANCE_FILE, maintenance_enabled
from homeassistant.components.habab_home.integration_setup import IntegrationSetup
from homeassistant.components.habab_home.xiaomi_setup import XiaomiSetup
import test_telegram_backend as fixtures


class MaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_startup_marker_skips_storage_worker_transport_and_config_flow(self):
        with tempfile.TemporaryDirectory(prefix="telegram-maintenance-") as root:
            marker = Path(root, MAINTENANCE_FILE)
            marker.touch()
            saved = Path(root, "fixture-pending-inputs")
            saved.write_bytes(b"synthetic-pending-inputs")
            hass = HomeAssistant(root)
            try:
                manager = TelegramManager(hass)
                hass.data["habab_telegram"] = manager
                manager.inbox.load = AsyncMock()
                manager.bindings_store.async_load = AsyncMock()
                manager.store.async_load = AsyncMock()
                with patch("homeassistant.components.habab_telegram.async_register_commands"), patch(
                    "homeassistant.components.habab_telegram.resolve_telegram_transport", new=AsyncMock()
                ) as resolve, patch(
                    "homeassistant.components.habab_telegram.asyncio.create_subprocess_exec", new=AsyncMock()
                ) as spawn:
                    await manager.setup()
                    await manager.async_setup_entry(SimpleNamespace(entry_id="entry", data={}))
                    manager._provider_config_changed()
                    await manager._reload_telegram_for_provider_change()
                resolve.assert_not_awaited()
                spawn.assert_not_awaited()
                manager.inbox.load.assert_not_awaited()
                manager.bindings_store.async_load.assert_not_awaited()
                manager.store.async_load.assert_not_awaited()
                self.assertEqual(manager.entries, {})
                self.assertEqual(manager.tasks, {})
                self.assertEqual(manager.consumer_tasks, {})
                self.assertIsNone(manager.provider_refresh_task)
                self.assertTrue((await manager.status("owner"))["maintenance"])
                flow = SimpleNamespace(hass=hass, async_abort=Mock(return_value={"type": "abort"}))
                with patch("homeassistant.components.habab_telegram.config_flow.get_bot", new=AsyncMock()) as get_bot:
                    await HababTelegramConfigFlow.async_step_user(flow, {"token": "fixture"})
                get_bot.assert_not_awaited()
                flow.async_abort.assert_called_once_with(reason="telegram_maintenance")
                self.assertEqual(saved.read_bytes(), b"synthetic-pending-inputs")
            finally:
                await hass.async_stop(force=True)

    async def test_existing_callbacks_cannot_send_ack_process_or_mutate_when_maintenance_is_on(self):
        manager = fixtures.TelegramManagerTests().manager()
        manager.maintenance = True
        entry = fixtures.TelegramManagerTests().group_entry()
        state = manager.entries[entry.entry_id]
        state["process"] = SimpleNamespace(stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()),
            stdout=SimpleNamespace(readline=AsyncMock()))
        manager._write = TelegramManager._write.__get__(manager)
        manager.send = TelegramManager.send.__get__(manager)
        manager.get_chat_member = TelegramManager.get_chat_member.__get__(manager)
        manager.queues[entry.entry_id] = asyncio.Queue()
        message = {"type": "message", "update_id": 1, "message_id": 1,
                   "chat_id": 10, "user_id": 20, "chat_type": "private", "text": "fixture"}
        manager.queues[entry.entry_id].put_nowait(message)
        self.assertFalse(await manager.send("entry", 10, "fixture"))
        self.assertFalse(await manager.typing("entry", 10))
        self.assertIsNone(await manager.get_chat_member("entry", -100, 20))
        self.assertFalse(await manager._write("entry", {"type": "ack_update", "update_id": 1}))
        self.assertFalse(await manager._receive_update(entry, message))
        await manager._reader(entry, state)
        await manager._consume(entry)
        await manager.handle_one(entry, message)
        await manager._handle_control(entry, message)
        manager._pump_inbox(entry)
        self.assertEqual(manager.queues[entry.entry_id].qsize(), 1)
        state["process"].stdin.write.assert_not_called()
        state["process"].stdout.readline.assert_not_awaited()
        manager.inbox.store.async_save.assert_not_awaited()
        manager.store.async_save.assert_not_awaited()
        manager.bindings_store.async_save.assert_not_awaited()
        manager.hass.data["habab_home"]["assistant"].run.assert_not_awaited()
        for call in (manager.configure("owner", "fixture"), manager.pair("owner"),
                     manager.group_pair("owner"), manager.unlink_group("owner"),
                     manager.unlink("owner"), manager.recover_inbox("owner", "a" * 64)):
            with self.assertRaisesRegex(ValueError, "telegram_maintenance"):
                await call

    async def test_startup_completion_paths_leave_notifications_pending(self):
        manager = fixtures.TelegramManagerTests().manager()
        manager.maintenance = True
        manager.hass.data["habab_telegram"] = manager
        manager.hass.config_entries = SimpleNamespace(async_get_entry=lambda _id: SimpleNamespace(
            state=SimpleNamespace(value="loaded"), domain="fixture"))
        integration = object.__new__(IntegrationSetup)
        integration.hass = manager.hass
        integration._require_active_admin = AsyncMock()
        record = {"owner": "owner", "entry_id": "fixture", "domain": "fixture",
                  "trusted_origin": {"channel": "telegram"}}
        await integration._notify_completion(record)
        self.assertFalse(record.get("completion_delivered", False))
        xiaomi = object.__new__(XiaomiSetup)
        xiaomi.hass = manager.hass
        xiaomi._notifying = set()
        xiaomi._lock = asyncio.Lock()
        xiaomi._save = AsyncMock()
        xiaomi.records = {"owner": {"phase": "completed", "entry_id": "fixture",
                                    "trusted_origin": {"channel": "telegram"}}}
        await xiaomi.retry_pending_completions()
        self.assertFalse(xiaomi.records["owner"].get("completion_delivered", False))
        xiaomi._save.assert_not_awaited()
        manager.send.assert_not_awaited()
        retries = SimpleNamespace(retry_pending_completions=Mock())
        manager.hass.data["habab_home"]["integration_setup"] = retries
        manager.hass.data["habab_home"]["xiaomi_setup"] = SimpleNamespace(retry_pending_completions=AsyncMock())
        await manager._retry_setup_notifications()
        retries.retry_pending_completions.assert_not_called()

    def test_absent_marker_is_default_and_removal_does_not_hot_resume_running_manager(self):
        with tempfile.TemporaryDirectory(prefix="telegram-marker-") as root:
            hass = SimpleNamespace(data={}, config=SimpleNamespace(path=lambda name: str(Path(root, name))))
            self.assertFalse(maintenance_enabled(hass))
            marker = Path(root, MAINTENANCE_FILE)
            marker.touch()
            self.assertTrue(maintenance_enabled(hass))
            hass.data["habab_telegram"] = SimpleNamespace(maintenance=True)
            marker.unlink()
            self.assertTrue(maintenance_enabled(hass))
            hass.data.clear()
            self.assertFalse(maintenance_enabled(hass))
