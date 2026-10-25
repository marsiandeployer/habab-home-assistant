import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from publish_build import PUBLISHED_ASSET_DIRS, PUBLISHED_ASSET_FILES, stage_published_frontend


class PublishedFrontendSelectionTests(unittest.TestCase):
    def test_stages_compiled_frontend_allowlist_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'public'
            root.mkdir()
            (root / 'index.html').write_text('scoped public index', encoding='utf-8')
            for name in PUBLISHED_ASSET_DIRS:
                directory = root / name
                directory.mkdir()
                (directory / 'asset.txt').write_text(name, encoding='utf-8')
            for name in PUBLISHED_ASSET_FILES:
                (root / name).write_text(name, encoding='utf-8')
            for name in ('downloads', 'assistant', 'previews', 'integrations'):
                directory = root / name
                directory.mkdir()
                (directory / 'must-not-copy.txt').write_text(name, encoding='utf-8')
            entrypoint = Path(temporary) / 'flat-entrypoint.html'
            entrypoint.write_text('<title>Home Assistant Demo</title>', encoding='utf-8')
            stage = Path(temporary) / 'stage'

            stage_published_frontend(root, stage, entrypoint)

            files = {str(path.relative_to(stage)) for path in stage.rglob('*') if path.is_file()}
            expected = {f'{name}/asset.txt' for name in PUBLISHED_ASSET_DIRS}
            expected.update(PUBLISHED_ASSET_FILES)
            expected.add('index.html')
            self.assertEqual(files, expected)
            self.assertEqual((stage / 'index.html').read_text(encoding='utf-8'), entrypoint.read_text(encoding='utf-8'))

    def test_missing_compiled_root_fails_before_creating_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'public'
            root.mkdir()
            for name in PUBLISHED_ASSET_DIRS[:-1]:
                (root / name).mkdir()
            for name in PUBLISHED_ASSET_FILES:
                (root / name).write_text(name, encoding='utf-8')
            entrypoint = Path(temporary) / 'flat-entrypoint.html'
            entrypoint.write_text('<title>Home Assistant Demo</title>', encoding='utf-8')
            stage = Path(temporary) / 'stage'

            with self.assertRaisesRegex(ValueError, 'stub_config'):
                stage_published_frontend(root, stage, entrypoint)
            self.assertFalse(stage.exists())

    def test_copy_failure_removes_only_partial_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'public'
            root.mkdir()
            public_index = root / 'index.html'
            public_index.write_text('current public index', encoding='utf-8')
            for name in PUBLISHED_ASSET_DIRS:
                directory = root / name
                directory.mkdir()
                (directory / 'asset.txt').write_text(name, encoding='utf-8')
            for name in PUBLISHED_ASSET_FILES:
                (root / name).write_text(name, encoding='utf-8')
            entrypoint = Path(temporary) / 'flat-entrypoint.html'
            entrypoint.write_text('<title>Home Assistant Demo</title>', encoding='utf-8')
            stage = Path(temporary) / 'stage'
            copytree = shutil.copytree

            def fail_on_later_root(source, destination, *args, **kwargs):
                if Path(source).name == 'frontend_latest':
                    raise OSError('fixture copy failure')
                return copytree(source, destination, *args, **kwargs)

            with patch('publish_build.shutil.copytree', side_effect=fail_on_later_root):
                with self.assertRaisesRegex(OSError, 'fixture copy failure'):
                    stage_published_frontend(root, stage, entrypoint)

            self.assertFalse(stage.exists())
            self.assertEqual(public_index.read_text(encoding='utf-8'), 'current public index')
            self.assertEqual((root / 'assets/asset.txt').read_text(encoding='utf-8'), 'assets')


if __name__ == '__main__':
    unittest.main()
