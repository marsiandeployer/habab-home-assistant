import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('device', Path(__file__).parents[1] / 'tools/device.py')
device = importlib.util.module_from_spec(spec)
spec.loader.exec_module(device)


class DeviceSelectionTest(unittest.TestCase):
    def test_no_device_or_multiple_devices_never_selects_implicitly(self):
        for output in ('List of devices attached\n', 'List of devices attached\na device\nb device\n'):
            with self.subTest(output=output), patch.object(device, 'run', return_value=output):
                with self.assertRaises(RuntimeError):
                    device.select_device()

    def test_unauthorized_device_never_receives_install_commands(self):
        with patch.object(device, 'run', return_value='List of devices attached\nphone unauthorized\n') as run:
            with self.assertRaisesRegex(RuntimeError, 'approve USB debugging'):
                device.select_device()
            self.assertEqual(run.call_count, 1)

    def test_other_huawei_model_is_rejected(self):
        with patch.object(device, 'run', side_effect=['List of devices attached\nphone device\n', 'OTHER']):
            with self.assertRaisesRegex(RuntimeError, 'Wrong device model'):
                device.select_device()

    def test_explicit_serial_and_correct_model(self):
        with patch.object(device, 'run', side_effect=['List of devices attached\nphone device\nother device\n', 'VOG-L29']):
            self.assertEqual(device.select_device('phone'), 'phone')

    def test_windows_adb_tunnel_uses_explicit_server_for_every_command(self):
        prefix = ['/opt/android-sdk/platform-tools/adb', '-H', '127.0.0.1', '-P', '18537']
        with patch.object(device, 'run', side_effect=['List of devices attached\nphone device\n', 'VOG-L29']) as run:
            self.assertEqual(device.select_device('phone', prefix), 'phone')
            self.assertEqual(run.call_args_list[0].args[0], prefix + ['devices'])
            self.assertEqual(run.call_args_list[1].args[0], prefix + ['-s', 'phone', 'shell', 'getprop', 'ro.product.model'])

    def test_runtime_check_excludes_proot_parent_and_never_returns_other_environment(self):
        command = 'python3 -P /usr/local/share/habab-android/launch.py --config /config'
        rows = 'PID ARGS\n10 libproot.so -0 /usr/local/bin/' + command + '\n11 ' + command
        with patch.object(device, 'run', return_value=rows), patch.object(device.subprocess, 'check_output', return_value=b'PRIVATE_TEST=value\0HABAB_RUNTIME_KIND=android_proot\0') as read:
            self.assertEqual(device.verify_runtime(['adb', '-s', 'phone']), {
                'result': 'PASS', 'pid': 11, 'runtimeKind': 'android_proot'})
            self.assertEqual(read.call_args.args[0][-1], '/proc/11/environ')

    def test_runtime_check_rejects_ambiguous_process_or_wrong_flag(self):
        command = 'python3 -P /usr/local/share/habab-android/launch.py --config /config'
        with patch.object(device, 'run', return_value='1 ' + command + '\n2 ' + command), patch.object(device.subprocess, 'check_output') as read:
            with self.assertRaisesRegex(RuntimeError, 'exactly one'):
                device.verify_runtime(['adb'])
            read.assert_not_called()
        with patch.object(device, 'run', return_value='1 ' + command), patch.object(device.subprocess, 'check_output', return_value=b'HABAB_RUNTIME_KIND=docker\0'):
            with self.assertRaisesRegex(RuntimeError, 'Android runtime context'):
                device.verify_runtime(['adb'])


if __name__ == '__main__':
    unittest.main()
