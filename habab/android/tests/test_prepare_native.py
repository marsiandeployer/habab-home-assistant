import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / 'tools' / 'prepare_native.py'
SPEC = importlib.util.spec_from_file_location('prepare_native', SCRIPT)
prepare_native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare_native)


class PackageFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cache = Path(self.temp.name)
        self.payload = b'pinned package bytes'
        self.record = {
            'Filename': 'pool/main/p/package.deb',
            'SHA256': hashlib.sha256(self.payload).hexdigest(),
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_offline_cache_uses_pinned_package_without_network(self):
        package = self.cache / 'package.deb'
        package.write_bytes(self.payload)

        with mock.patch.object(prepare_native.urllib.request, 'urlretrieve') as download:
            result = prepare_native.package_file('package', self.record, self.cache, offline=True)

        self.assertEqual(result, package)
        download.assert_not_called()

    def test_offline_cache_fails_when_package_is_missing(self):
        with mock.patch.object(prepare_native.urllib.request, 'urlretrieve') as download:
            with self.assertRaisesRegex(FileNotFoundError, 'Missing offline native package cache entry'):
                prepare_native.package_file('package', self.record, self.cache, offline=True)

        download.assert_not_called()

    def test_offline_cache_rejects_unpinned_package_bytes(self):
        (self.cache / 'package.deb').write_bytes(b'not the pinned package')

        with mock.patch.object(prepare_native.urllib.request, 'urlretrieve') as download:
            with self.assertRaisesRegex(ValueError, 'Package checksum mismatch: package'):
                prepare_native.package_file('package', self.record, self.cache, offline=True)

        download.assert_not_called()

    def test_default_cache_downloads_then_checks_pinned_package(self):
        def download(_url, destination):
            Path(destination).write_bytes(self.payload)

        with mock.patch.object(prepare_native.urllib.request, 'urlretrieve', side_effect=download) as fetch:
            result = prepare_native.package_file('package', self.record, self.cache)

        self.assertEqual(result.read_bytes(), self.payload)
        fetch.assert_called_once_with(
            'https://packages.termux.dev/apt/termux-main/pool/main/p/package.deb', result)


if __name__ == '__main__':
    unittest.main()
