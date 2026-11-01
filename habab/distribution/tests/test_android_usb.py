"""Validate the Android USB status sidecar and native serial-port integration."""

import json
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components import usb
from homeassistant.components.usb import SerialDevice

from homeassistant.components.habab_home import android_usb
from homeassistant.components.habab_home.runtime_context import ANDROID_PROOT_RUNTIME_KIND


def status(*, updated_at=1000, connected=True, permission_granted=True,
           bridge_ready=False, **changes):
    result = {
        "schema": 1,
        "updated_at": updated_at,
        "supported": True,
        "connected": connected,
        "permission_granted": permission_granted,
        "bridge_ready": bridge_ready,
        "vid": 1105,
        "pid": 5800,
        "device_path": android_usb.USB_DEVICE_PATH,
        "radio_type": "znp",
        "status": "ready" if bridge_ready else "permission_required" if not permission_granted else "waiting",
        "message": "Мост готов" if bridge_ready else "Ожидаю USB-разрешение",
        "product": "CC2531",
    }
    result.update(changes)
    return result


class AndroidUsbStatusTests(unittest.TestCase):
    def read(self, path, *, socket_path=None, now=1000):
        return android_usb.read_usb_status(
            path=path,
            socket_path=socket_path or path.parent / "cc2531.sock",
            now=now,
            runtime_kind=ANDROID_PROOT_RUNTIME_KIND,
        )

    def write(self, path, payload):
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_desktop_runtime_and_missing_android_file_are_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usb-status.json"
            self.assertEqual(android_usb.read_usb_status(
                path=path, runtime_kind="docker"),
                {"status": "unavailable", "reason": "not_android_runtime"})
            self.assertEqual(self.read(path),
                             {"status": "unavailable", "reason": "status_file_missing"})

    def test_malformed_oversized_and_stale_status_are_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usb-status.json"
            path.write_text("{broken", encoding="utf-8")
            self.assertEqual(self.read(path)["reason"], "status_file_invalid")
            path.write_bytes(b" " * (android_usb.MAX_STATUS_BYTES + 1))
            self.assertEqual(self.read(path)["reason"], "status_too_large")
            self.write(path, status(updated_at=900))
            self.assertEqual(self.read(path)["reason"], "status_stale")
            self.write(path, status(updated_at=1100))
            self.assertEqual(self.read(path, now=1000)["reason"], "status_stale")

    def test_rejects_wrong_device_contract_and_impossible_ready_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usb-status.json"
            for change in ({"schema": 2}, {"supported": False}, {"vid": 1},
                           {"vid": True}, {"updated_at": 10 ** 1000},
                           {"device_path": "/dev/ttyACM0"},
                           {"bridge_ready": True, "connected": False},
                           {"bridge_ready": True, "permission_granted": False},
                           {"status": "ready\nclaim"}, {"message": "x" * 257}):
                with self.subTest(change=change):
                    payload = status(**change)
                    self.write(path, payload)
                    self.assertEqual(self.read(path)["reason"], "status_contract_invalid")

    def test_permission_waiting_is_reported_but_not_advertised_as_serial_port(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usb-status.json"
            self.write(path, status(permission_granted=False))
            result = self.read(path)
            self.assertEqual(result["status"], "permission_required")
            self.assertFalse(result["bridge_ready"])
            with patch.object(android_usb, "read_usb_status", return_value=result):
                self.assertEqual(android_usb.scan_serial_ports(None), [])

    def test_ready_status_requires_real_unix_socket_and_registers_serialdevice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "usb-status.json"
            socket_path = root / "cc2531.sock"
            self.write(path, status(bridge_ready=True))
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(socket_path))
                result = self.read(path, socket_path=socket_path)
                self.assertTrue(result["bridge_ready"])
                self.assertEqual(result["device_path"], android_usb.USB_DEVICE_PATH)
                with patch.object(android_usb, "read_usb_status", return_value=result):
                    ports = android_usb.scan_serial_ports(None)
                self.assertEqual(len(ports), 1)
                self.assertIsInstance(ports[0], SerialDevice)
                self.assertEqual(ports[0].device, android_usb.USB_DEVICE_PATH)
                self.assertEqual(ports[0].manufacturer, "Texas Instruments")

            socket_path.unlink()
            disconnected = self.read(path, socket_path=socket_path)
            self.assertEqual(disconnected["reason"],
                             "bridge_socket_missing")
            with patch.object(android_usb, "read_usb_status",
                              return_value=self.read(path, socket_path=socket_path)):
                self.assertEqual(android_usb.scan_serial_ports(None), [])

    async def _async_setup_registers_only_for_android_and_unregisters_on_stop(self):
        hass = SimpleNamespace(
            config=SimpleNamespace(components=set()),
            bus=SimpleNamespace(async_listen_once=Mock()),
        )
        remove = Mock()
        with patch.dict("os.environ", {"HABAB_RUNTIME_KIND": ANDROID_PROOT_RUNTIME_KIND}), \
             patch.object(android_usb, "async_setup_component", new=AsyncMock(return_value=True)), \
             patch.object(usb, "async_register_serial_port_scanner", return_value=remove) as register:
            self.assertTrue(await android_usb.async_setup(hass))
            self.assertIs(register.call_args.args[1], android_usb.scan_serial_ports)
            stop_callback = hass.bus.async_listen_once.call_args.args[1]
            stop_callback(None)
            remove.assert_called_once_with()

    def test_android_setup_registers_scanner_and_removes_it_on_shutdown(self):
        import asyncio

        asyncio.run(self._async_setup_registers_only_for_android_and_unregisters_on_stop())

    def test_desktop_setup_does_not_load_usb_integration(self):
        import asyncio

        hass = SimpleNamespace(config=SimpleNamespace(components=set()))
        with patch.dict("os.environ", {"HABAB_RUNTIME_KIND": "docker"}), \
             patch.object(android_usb, "async_setup_component", new=AsyncMock()) as setup_component:
            self.assertTrue(asyncio.run(android_usb.async_setup(hass)))
        setup_component.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
