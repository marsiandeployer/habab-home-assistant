import hashlib
from pathlib import Path
import tempfile
import unittest

from package_runtime import install_serialx_unix


class SerialxUnixRuntimeOverlayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rootfs = Path(self.temp.name)
        self.site = self.rootfs / 'usr/local/lib/python3.14/site-packages'
        self.platforms = self.site / 'serialx/platforms'
        self.platforms.mkdir(parents=True)
        self.metadata = self.site / 'serialx-1.11.0.dist-info/METADATA'
        self.metadata.parent.mkdir()
        self.metadata.write_text(
            'Metadata-Version: 2.1\nName: serialx\nVersion: 1.11.0\n'
        )
        (self.platforms / '__init__.py').write_text(
            'from . import (\n    serial_socket,  # noqa: F401\n)\n'
        )

    def test_installs_pinned_overlay_and_is_idempotent(self):
        first = install_serialx_unix(self.rootfs)
        second = install_serialx_unix(self.rootfs)

        init_file = self.platforms / '__init__.py'
        unix_file = self.platforms / 'serial_unix.py'
        self.assertEqual(first, second)
        self.assertEqual(
            init_file.read_text().count('serial_unix,  # noqa: F401'), 1
        )
        self.assertEqual(
            first['files'][str(init_file.relative_to(self.rootfs))],
            hashlib.sha256(init_file.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            first['files'][str(unix_file.relative_to(self.rootfs))],
            hashlib.sha256(unix_file.read_bytes()).hexdigest(),
        )
        self.assertEqual(first['packageVersion'], '1.11.0')

    def test_rejects_unpinned_serialx_version(self):
        self.metadata.write_text(
            'Metadata-Version: 2.1\nName: serialx\nVersion: 1.10.0\n'
        )

        with self.assertRaisesRegex(
            ValueError, 'exactly one pinned serialx 1.11.0'
        ):
            install_serialx_unix(self.rootfs)


if __name__ == '__main__':
    unittest.main()
