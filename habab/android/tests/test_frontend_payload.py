import importlib.util
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('package_runtime', Path(__file__).parents[1] / 'tools/package_runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class FrontendPayloadTest(unittest.TestCase):
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
