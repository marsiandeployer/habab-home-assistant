"""Test private, key-separated config generation without runtime files."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from prepare_demo_realtime_config import build_config, write_config


class DemoRealtimeConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / 'voice-config.json'
        self.source.write_text(json.dumps({
            'key_sha256': 'a' * 64,
            'port': 8326,
            'browser_origins': ['https://habab-demo.wpmix.net', 'https://another.invalid'],
        }), encoding='utf-8')
        os.chmod(self.source, 0o600)

    def tearDown(self):
        self.temporary.cleanup()

    def test_config_uses_exact_origin_new_port_and_bounded_quotas(self):
        config = build_config(self.source)
        self.assertEqual(config, {
            'key_sha256': 'a' * 64,
            'port': 18329,
            'browser_origins': ['https://habab-demo.wpmix.net'],
            'demo_openai_session_limit': 5,
            'demo_openai_sessions_per_session': 2,
            'demo_openai_session_window_seconds': 86400,
        })

    def test_write_creates_new_private_directory_and_config(self):
        destination = self.root / 'dedicated' / 'config.json'
        result = write_config(self.source, destination)
        self.assertEqual(result, destination)
        self.assertEqual(stat.S_IMODE(destination.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        self.assertEqual(json.loads(destination.read_text(encoding='utf-8')),
                         build_config(self.source))
        with self.assertRaises(FileExistsError):
            write_config(self.source, destination)
        self.assertEqual(json.loads(destination.read_text(encoding='utf-8')),
                         build_config(self.source))

    def test_write_allows_new_versioned_config_in_existing_private_directory(self):
        destination_dir = self.root / 'dedicated'
        destination_dir.mkdir(mode=0o700)
        destination = destination_dir / 'config-2ca4.json'
        write_config(self.source, destination)
        self.assertEqual(stat.S_IMODE(destination_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        with self.assertRaises(FileExistsError):
            write_config(self.source, destination)
        self.assertEqual(json.loads(destination.read_text(encoding='utf-8')),
                         build_config(self.source))

    def test_rejects_non_private_source_config(self):
        os.chmod(self.source, 0o644)
        with self.assertRaises(PermissionError):
            build_config(self.source)


if __name__ == '__main__':
    unittest.main()
