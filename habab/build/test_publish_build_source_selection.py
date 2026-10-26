import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from publish_build import (
    PUBLISHED_ASSET_DIRS,
    PUBLISHED_ASSET_FILES,
    UPSTREAM_BASELINE,
    attribution_text,
    copy_public_release,
    replace_public_index,
    scope_demo_realtime_headers,
    stage_published_frontend,
    validate_source_options,
    write_provenance,
)


class PublishedFrontendSelectionTests(unittest.TestCase):
    def test_demo_fallback_csp_allows_realtime_websocket(self):
        headers = scope_demo_realtime_headers(
            "Content-Security-Policy: default-src 'self'; connect-src 'self';\n"
        )
        self.assertIn("connect-src 'self' wss://habab-voice.wpmix.net;", headers)

    def test_demo_realtime_websocket_is_allowed_by_tracked_csp(self):
        repository = Path(__file__).resolve().parents[2]
        policies = [
            (repository / 'demo/public/_headers').read_text(encoding='utf-8'),
            (repository / 'habab/deploy/nginx-location.conf').read_text(encoding='utf-8'),
            (repository / 'habab/deploy/nginx-vhost.conf').read_text(encoding='utf-8'),
        ]
        for policy in policies:
            connect_src = re.search(r'connect-src ([^;\n]+)', policy)
            self.assertIsNotNone(connect_src)
            self.assertIn('wss://habab-voice.wpmix.net', connect_src.group(1))

    def test_demo_nginx_proxies_realtime_bootstrap(self):
        vhost = (Path(__file__).resolve().parents[1] / 'deploy/nginx-vhost.conf').read_text(encoding='utf-8')
        match = re.search(
            r'location = /api/ha-demo/realtime/bootstrap\s*\{([^}]*)\}',
            vhost,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        self.assertIn('proxy_pass http://127.0.0.1:8318;', match.group(1))
        self.assertIn('proxy_set_header X-Forwarded-Proto https;', match.group(1))

    def test_public_permissions_survive_restrictive_umask_without_changing_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            compiled = root / 'compiled'
            assets = compiled / 'assets'
            assets.mkdir(parents=True)
            index = compiled / 'index.html'
            manifest = compiled / 'manifest.json'
            bundle = assets / 'main.js'
            index.write_bytes(b'<title>compiled</title>')
            manifest.write_bytes(b'{"name":"Habab Home"}')
            bundle.write_bytes(b'compiled bundle')
            os.chmod(compiled, 0o700)
            os.chmod(assets, 0o700)
            for path in (index, manifest, bundle):
                os.chmod(path, 0o600)
            source_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in (index, manifest, bundle)}
            target = root / 'public'
            release = target / 'releases' / 'title1'

            old_umask = os.umask(0o077)
            try:
                copy_public_release(compiled, release, target)
                replace_public_index(index, target)
            finally:
                os.umask(old_umask)

            self.assertEqual(stat.S_IMODE(compiled.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(assets.stat().st_mode), 0o700)
            self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o600
                                for path in (index, manifest, bundle)))
            self.assertEqual(stat.S_IMODE((target / 'index.html').stat().st_mode), 0o644)
            directories = [path for path in release.rglob('*') if path.is_dir()]
            files = [path for path in release.rglob('*') if path.is_file()]
            public_directories = [target, target / 'releases', release, *directories]
            self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o755
                                for path in public_directories))
            self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o644 for path in files))
            for source, digest in source_hashes.items():
                copied = target / 'index.html' if source == index else release / source.relative_to(compiled)
                self.assertEqual(hashlib.sha256(copied.read_bytes()).hexdigest(), digest)
                self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)

    def test_external_compiled_source_stages_allowlist_and_trusted_license(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'compiled'
            root.mkdir()
            (root / 'index.html').write_text('<title>Home Assistant Demo</title>', encoding='utf-8')
            for name in PUBLISHED_ASSET_DIRS:
                directory = root / name
                directory.mkdir()
                (directory / 'asset.txt').write_text(name, encoding='utf-8')
            for name in ('_headers', 'manifest.json'):
                (root / name).write_text(name, encoding='utf-8')
            (root / 'downloads').mkdir()
            (root / 'downloads/extra.bin').write_bytes(b'not an input')
            upstream_license = Path(temporary) / 'trusted-LICENSE.md'
            upstream_license.write_text('trusted Apache license', encoding='utf-8')
            stage = Path(temporary) / 'stage'

            stage_published_frontend(
                root, stage, root / 'index.html', license_source=upstream_license)

            files = {str(path.relative_to(stage)) for path in stage.rglob('*') if path.is_file()}
            self.assertEqual(
                files,
                {f'{name}/asset.txt' for name in PUBLISHED_ASSET_DIRS}
                | {'_headers', 'manifest.json', 'LICENSE.md', 'index.html'},
            )
            self.assertEqual((stage / 'LICENSE.md').read_text(encoding='utf-8'), 'trusted Apache license')
            self.assertEqual((root / 'index.html').read_text(encoding='utf-8'), '<title>Home Assistant Demo</title>')

    def test_external_source_requires_full_revision_and_rejects_conflicting_modes(self):
        revision = 'e5babaf2ad473dd0282b959a4ef2863aacdbf3fb'
        source = Path('/compiled')
        self.assertEqual(
            validate_source_options(
                from_published=False, source_dir=source, entrypoint=None,
                frontend_revision=revision),
            revision,
        )
        for options, error in (
            ({'from_published': False, 'source_dir': source, 'entrypoint': None, 'frontend_revision': None},
             'requires --frontend-source-revision'),
            ({'from_published': False, 'source_dir': source, 'entrypoint': None, 'frontend_revision': 'bad'},
             'full lowercase Git SHA-1'),
            ({'from_published': True, 'source_dir': source, 'entrypoint': None, 'frontend_revision': revision},
             'mutually exclusive'),
            ({'from_published': False, 'source_dir': source, 'entrypoint': Path('/custom/index.html'), 'frontend_revision': revision},
             'cannot be combined'),
            ({'from_published': False, 'source_dir': None, 'entrypoint': None, 'frontend_revision': revision},
             'requires --source-dir'),
        ):
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                validate_source_options(**options)

    def test_attribution_and_provenance_separate_fork_from_upstream_baseline(self):
        revision = 'e5babaf2ad473dd0282b959a4ef2863aacdbf3fb'
        self.assertIn(f'source revision `{revision}`', attribution_text(revision))
        self.assertIn(f'upstream baseline is commit `{UPSTREAM_BASELINE}`', attribution_text(revision))
        self.assertIn('source revision `unknown`', attribution_text(None))
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            write_provenance(stage, revision)
            data = json.loads((stage / 'BUILD-PROVENANCE.json').read_text(encoding='utf-8'))
            self.assertEqual(data, {
                'frontend_source_revision': revision,
                'home_assistant_upstream_baseline': UPSTREAM_BASELINE,
            })
            self.assertNotIn(temporary, (stage / 'BUILD-PROVENANCE.json').read_text(encoding='utf-8'))

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
