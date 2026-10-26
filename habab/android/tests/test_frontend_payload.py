import importlib.util
from pathlib import Path
import re
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('package_runtime', Path(__file__).parents[1] / 'tools/package_runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class FrontendPayloadTest(unittest.TestCase):
    def test_actual_android_seed_preserves_shared_automation_without_duplicate_keys(self):
        config = runtime.initial_configuration()
        keys = re.findall(r'(?m)^([a-z_]+):', config)
        self.assertEqual(len(keys), len(set(keys)), 'Duplicate top-level keys in actual Android seed')
        self.assertEqual(keys.count('automation'), 1)
        self.assertIn('automation: !include automations.yaml', config)
        self.assertIn('input_boolean', keys)
        self.assertTrue({'script', 'scene', 'recorder', 'history'}.issubset(keys))

    def test_copy_sources_includes_vtt_captions_in_signed_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            distribution = temporary / 'distribution'
            caption = distribution / 'src/habab_home/frontend/telegram-guide.vtt'
            caption.parent.mkdir(parents=True)
            caption.write_bytes(b'WEBVTT\n\n')
            for name in ('habab-ha.py', 'install-xiaomi-home.py', 'xiaomi-home-release.json'):
                (distribution / name).write_text(name)
            rootfs = temporary / 'rootfs'
            with patch.object(runtime, 'DIST', distribution):
                inventory = runtime.copy_sources(rootfs)
            target = rootfs / 'usr/src/homeassistant/homeassistant/components/habab_home/frontend/telegram-guide.vtt'
            self.assertEqual(target.read_bytes(), b'WEBVTT\n\n')
            self.assertIn('src/habab_home/frontend/telegram-guide.vtt', inventory)
            self.assertEqual(inventory['src/habab_home/frontend/telegram-guide.vtt'], runtime.sha256(target))

    def test_cold_build_refuses_space_that_only_suffices_for_a_warm_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            rootfs = cache / 'rootfs'
            with patch.object(runtime.shutil, 'disk_usage', return_value=SimpleNamespace(free=3 * 1024 ** 3)):
                with self.assertRaises(OSError):
                    runtime.require_build_space(cache, rootfs)
                self.assertFalse(rootfs.exists())
                rootfs.mkdir()
                runtime.require_build_space(cache, rootfs)
            with patch.object(runtime.shutil, 'disk_usage', return_value=SimpleNamespace(free=0)):
                with self.assertRaises(OSError):
                    runtime.require_build_space(cache, rootfs)

    def test_signed_payload_changes_when_bytes_paths_or_metadata_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frontend = root / 'site/hass_frontend'
            metadata = root / 'site/frontend.dist-info'
            frontend.mkdir(parents=True)
            metadata.mkdir()
            script = frontend / 'entry.js'
            script.write_text('original')
            record = metadata / 'METADATA'
            record.write_text('Version: accepted')
            prefixes = ['site/hass_frontend', 'site/frontend.dist-info']
            count, original = runtime.frontend_payload(root, prefixes)
            self.assertEqual(count, 2)
            script.write_text('modified')
            self.assertNotEqual(runtime.frontend_payload(root, prefixes)[1], original)
            script.write_text('original')
            script.rename(frontend / 'other.js')
            self.assertNotEqual(runtime.frontend_payload(root, prefixes)[1], original)
            (frontend / 'other.js').rename(script)
            record.write_text('Version: another')
            self.assertNotEqual(runtime.frontend_payload(root, prefixes)[1], original)

    def test_missing_directories_and_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                runtime.frontend_payload(root, ['missing'])
            (root / 'frontend').mkdir()
            (root / 'outside').write_text('unrelated')
            (root / 'frontend/alias').symlink_to(root / 'outside')
            with self.assertRaises(ValueError):
                runtime.frontend_payload(root, ['frontend'])
