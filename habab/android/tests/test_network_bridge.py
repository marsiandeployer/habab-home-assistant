import errno
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest

spec = importlib.util.spec_from_file_location('launch', Path(__file__).parents[1] / 'runtime/launch.py')
launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch)


class NetworkBridgeTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'network.json'
        self.data = {'schema': 1, 'updatedAt': time.time(), 'interfaces': [
            {'name': 'wlan0', 'index': 7, 'addresses': [{'address': '192.168.1.5', 'prefix': 24},
                                                     {'address': 'fe80::123', 'prefix': 64}]}]}
        self.path.write_text(json.dumps(self.data))

    def module(self, native):
        return SimpleNamespace(get_adapters=native, IP=lambda **data: SimpleNamespace(**data),
                               Adapter=lambda **data: SimpleNamespace(**data))

    def test_native_success_is_untouched(self):
        module = self.module(lambda: ['native'])
        launch.install_bridge(module, self.path)
        self.assertEqual(module.get_adapters(), ['native'])

    def test_permission_denial_uses_actual_addresses_and_ipv6_scope(self):
        def denied(): raise PermissionError(errno.EPERM, 'denied')
        module = self.module(denied)
        launch.install_bridge(module, self.path)
        adapter = module.get_adapters()[0]
        self.assertEqual(adapter.index, 7)
        self.assertEqual(adapter.ips[0].ip, '192.168.1.5')
        self.assertEqual(adapter.ips[1].ip, ('fe80::123', 0, 7))

    def test_other_os_errors_are_not_suppressed(self):
        def failed(): raise OSError(errno.EIO, 'io error')
        module = self.module(failed)
        launch.install_bridge(module, self.path)
        with self.assertRaisesRegex(OSError, 'io error'):
            module.get_adapters()

    def test_stale_missing_empty_and_malformed_snapshots_fail(self):
        for change in ({'updatedAt': 0}, {'interfaces': []}, {'schema': 2}):
            with self.subTest(change=change):
                self.path.write_text(json.dumps(dict(self.data, **change)))
                with self.assertRaises(ValueError):
                    launch.adapters_from_snapshot(self.module(lambda: []), self.path)
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            launch.adapters_from_snapshot(self.module(lambda: []), self.path)


if __name__ == '__main__':
    unittest.main()
