"""Protect the release boundary between compiled frontend, runtime and APK."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import zipfile


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


HABAB = Path(__file__).resolve().parents[2]
candidate = module('frontend_candidate', HABAB / 'native-frontend/candidate.py')
verifier = module('frontend_verifier', HABAB / 'android/tools/verify_runtime.py')
runtime = module('frontend_runtime', HABAB / 'android/tools/package_runtime.py')


class FrontendCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.revision = 'a' * 40
        self.version = '20260930.4+habab.14'
        self.wheel = self.root / f'home_assistant_frontend-{self.version}-py3-none-any.whl'
        with zipfile.ZipFile(self.wheel, 'w') as wheel:
            wheel.writestr('hass_frontend/index.html', 'fresh branding')
        self.manifest = {
            'source_revision': self.revision,
            'source_clean_before_build': True,
            'builder_checks': 'passed',
            'wheel_version': self.version,
            'artifact': self.wheel.name,
            'artifact_bytes': self.wheel.stat().st_size,
            'artifact_sha256': hashlib.sha256(self.wheel.read_bytes()).hexdigest(),
        }
        self.write_manifest()

    def write_manifest(self):
        (self.root / 'manifest.json').write_text(json.dumps(self.manifest))

    def test_candidate_binds_compiled_bytes_and_clean_source_to_runtime(self):
        _, wheel, provenance = candidate.load_candidate(self.root, self.revision)
        self.assertEqual(wheel, self.wheel)
        release = {'nativeFrontend': provenance}
        verifier.validate_frontend_candidate(release, self.root, self.revision)
        for field in ('sourceRevision', 'wheelSha256', 'producerManifestSha256', 'version'):
            with self.subTest(field=field):
                changed = {**provenance, field: 'stale'}
                with self.assertRaisesRegex(ValueError, 'freshly built wheel'):
                    verifier.validate_frontend_candidate({'nativeFrontend': changed}, self.root, self.revision)

    def test_old_source_dirty_build_or_missing_checks_are_rejected(self):
        for field, value in (('source_revision', 'b' * 40), ('source_clean_before_build', False),
                             ('builder_checks', 'not run')):
            with self.subTest(field=field):
                original = self.manifest[field]
                self.manifest[field] = value
                self.write_manifest()
                with self.assertRaisesRegex(ValueError, 'exact clean source'):
                    candidate.load_candidate(self.root, self.revision)
                self.manifest[field] = original

    def test_modified_missing_or_relabelled_wheel_is_rejected(self):
        self.wheel.write_bytes(b'old frontend')
        with self.assertRaisesRegex(ValueError, 'integrity mismatch'):
            candidate.load_candidate(self.root, self.revision)
        self.wheel.unlink()
        with self.assertRaisesRegex(ValueError, 'integrity mismatch'):
            candidate.load_candidate(self.root, self.revision)
        self.manifest['artifact'] = '../old.whl'
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, 'filename'):
            candidate.load_candidate(self.root, self.revision)

    def test_runtime_installs_the_actual_local_wheel_without_download_or_pointer(self):
        rootfs = self.root / 'rootfs'
        core = rootfs / 'usr/src/homeassistant/homeassistant/components/frontend/manifest.json'
        core.parent.mkdir(parents=True)
        core.write_text(json.dumps({'requirements': ['home-assistant-frontend==20260930.4']}))
        site = rootfs / 'usr/local/lib/python3.14/site-packages'
        site.mkdir(parents=True)
        def install(command, **_):
            self.assertEqual(command[-1], str(self.wheel))
            self.assertIn('--no-index', command)
            for prefix in ('hass_frontend', f'home_assistant_frontend-{self.version}.dist-info'):
                target = site / prefix
                target.mkdir()
                (target / 'data').write_text('fresh compiled payload')
        with patch.object(runtime.subprocess, 'run', side_effect=install) as pip:
            frontend = runtime.install_frontend(rootfs, self.root, self.revision)
            self.assertEqual(frontend['wheelSha256'], self.manifest['artifact_sha256'])
            self.assertEqual(frontend['sourceRevision'], self.revision)
            self.assertEqual(frontend['payloadFiles'], 2)
            self.assertEqual(pip.call_count, 1)
        with patch.object(runtime.subprocess, 'run') as pip:
            with self.assertRaisesRegex(ValueError, 'exact clean source'):
                runtime.install_frontend(rootfs, self.root, 'b' * 40)
            pip.assert_not_called()

    def test_signed_apk_readback_rejects_a_stale_runtime_or_frontend(self):
        _, _, provenance = candidate.load_candidate(self.root, self.revision)
        release = {'sourceRevision': self.revision, 'id': 'runtime-fresh', 'nativeFrontend': provenance,
                   'url': 'https://habab-demo.wpmix.net/downloads/android/habab-runtime-fresh-arm64.tgz'}
        manifest = self.root / 'release.json'
        manifest.write_text(json.dumps(release))
        embedded = verifier.apk_provenance(release, manifest)
        apk = self.root / 'app.apk'
        def write_apk(value, default_url=None):
            with zipfile.ZipFile(apk, 'w') as archive:
                archive.writestr('assets/release/build-provenance.json', json.dumps(value))
                archive.writestr('assets/release/default-url.txt', default_url or verifier.runtime_manifest_url(release))
        write_apk(embedded)
        verifier.verify_apk_provenance(apk, release, manifest)
        write_apk({**embedded, 'runtimeId': 'old-runtime'})
        with self.assertRaisesRegex(ValueError, 'Signed APK'):
            verifier.verify_apk_provenance(apk, release, manifest)

        write_apk(embedded, 'https://habab-demo.wpmix.net/downloads/android/release-old.json')
        with self.assertRaisesRegex(ValueError, 'download URL'):
            verifier.verify_apk_provenance(apk, release, manifest)
        write_apk(embedded)
        manifest.write_text(manifest.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, 'Signed APK'):
            verifier.verify_apk_provenance(apk, release, manifest)

    def test_workflow_writes_valid_frontend_provenance_for_the_apk(self):
        workflow = (HABAB.parent / '.github/workflows/habab-release-candidate.yaml').read_text()
        blocks = re.findall(r"(?ms)^[ \t]+python3 - <<'PY'\n(.*?)^[ \t]+PY[ \t]*$", workflow)
        writer = next(textwrap.dedent(block) for block in blocks if "provenance = verifier['apk_provenance']" in block)
        tools = self.root / 'habab/android/tools'
        tools.mkdir(parents=True)
        (tools / 'verify_runtime.py').symlink_to(HABAB / 'android/tools/verify_runtime.py')
        assets = self.root / 'habab/android/app/src/main/assets/release'
        assets.mkdir(parents=True)
        dist = self.root / 'habab/android/dist'
        dist.mkdir()
        _, _, provenance = candidate.load_candidate(self.root, self.revision)
        release = {'sourceRevision': self.revision, 'id': 'fresh', 'nativeFrontend': provenance,
                   'url': 'https://habab-demo.wpmix.net/downloads/android/habab-runtime-fresh-arm64.tgz'}
        manifest = dist / 'release.json'
        manifest.write_text(json.dumps(release))
        Path(str(manifest) + '.sig').write_text('synthetic signature')
        (assets / 'default-url.txt').write_text('https://habab-demo.wpmix.net/downloads/android/release-old.json')
        subprocess.run([sys.executable, '-c', writer], cwd=self.root, check=True,
                       capture_output=True, text=True)
        generated = json.loads((assets / 'build-provenance.json').read_text())
        self.assertEqual(generated, verifier.apk_provenance(release, manifest))
        self.assertEqual((assets / 'default-url.txt').read_text().strip(),
                         'https://habab-demo.wpmix.net/downloads/android/release-fresh.json')
        self.assertEqual((self.root / 'candidate/android/release-fresh.json').read_bytes(), manifest.read_bytes())
        self.assertEqual((self.root / 'candidate/android/release-fresh.json.sig').read_text(), 'synthetic signature')

    def test_actual_apk_badging_must_equal_the_explicit_release_version(self):
        accepted = "package: name='ru.habab.home' versionCode='34' versionName='0.2.4-preview' platformBuildVersionName='14'\n"
        with patch.object(verifier.subprocess, 'run', return_value=SimpleNamespace(stdout=accepted)) as aapt:
            result = verifier.verify_apk_version(self.root / 'app.apk', '/sdk/aapt', '34', '0.2.4-preview')
            self.assertEqual(result, {'version_code': 34, 'version_name': '0.2.4-preview'})
            self.assertEqual(aapt.call_args.args[0][:3], ['/sdk/aapt', 'dump', 'badging'])
            with self.assertRaisesRegex(ValueError, 'explicit release inputs'):
                verifier.verify_apk_version(self.root / 'app.apk', '/sdk/aapt', '35', '0.2.4-preview')
            with self.assertRaisesRegex(ValueError, 'explicit release inputs'):
                verifier.verify_apk_version(self.root / 'app.apk', '/sdk/aapt', '34', 'wrong-name')
        for badging in ('', accepted.replace('ru.habab.home', 'wrong.application')):
            with patch.object(verifier.subprocess, 'run', return_value=SimpleNamespace(stdout=badging)):
                with self.assertRaisesRegex(ValueError, 'explicit release inputs'):
                    verifier.verify_apk_version(self.root / 'app.apk', '/sdk/aapt', '34', '0.2.4-preview')
