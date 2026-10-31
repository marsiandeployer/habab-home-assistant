import importlib.util
import json
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('prepare_apk_only', Path(__file__).with_name('prepare_apk_only.py'))
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)
VERIFY_SPEC = importlib.util.spec_from_file_location('verify_runtime', Path(__file__).with_name('verify_runtime.py'))
verify_runtime = importlib.util.module_from_spec(VERIFY_SPEC)
VERIFY_SPEC.loader.exec_module(verify_runtime)


class ApkOnlyReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.configure_git_identity(self.repo)
        self.write('habab/android/app/src/main/AndroidManifest.xml', '<manifest />')
        self.write('habab/android/app/src/main/java/App.java', 'class App {}')
        self.write('habab/native-frontend/src/app.ts', 'const app = true;')
        self.write('habab/distribution/src/habab_home/agent.py', 'pass\n')
        self.write('habab/distribution/Dockerfile', 'FROM ghcr.io/example/helper@sha256:abc\n')
        self.write('habab/android/tools/package_runtime.py',
                   "IMAGE = 'ghcr.io/example/runtime@sha256:def'\n"
                   "NODE_APK_HELPER = 'ghcr.io/example/helper@sha256:abc'\n")
        self.write('habab/product-tunnel/web_tunnel.py', 'pass\n')
        self.write('README.md', 'release notes\n')
        self.commit('base')
        self.base = self.git('rev-parse', 'HEAD')

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative, content):
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    @staticmethod
    def configure_git_identity(repo):
        subprocess.run(['git', '-C', str(repo), 'config', 'user.email', 'test@example.invalid'], check=True)
        subprocess.run(['git', '-C', str(repo), 'config', 'user.name', 'APK Candidate Test'], check=True)

    def commit(self, message):
        self.git('add', '.')
        self.git('commit', '-m', message)

    def changed_revision(self, path, content):
        self.write(path, content)
        self.commit('change')
        return self.git('rev-parse', 'HEAD')

    def signed_runtime_expectations(self):
        producer = self.repo / 'habab/android/tools/package_runtime.py'
        return {'baseImage': 'ghcr.io/example/runtime@sha256:def',
                'producerSha256': __import__('hashlib').sha256(producer.read_bytes()).hexdigest()}

    def test_native_java_res_and_manifest_edits_are_allowed(self):
        self.write('habab/android/app/src/main/java/App.java', 'class App { int value = 2; }')
        self.write('habab/android/app/src/main/res/values/strings.xml', '<resources />')
        self.write('habab/android/app/src/main/AndroidManifest.xml', '<manifest version="2" />')
        self.write('habab/android/app/src/test/java/VoiceTest.java', 'class VoiceTest {}')
        self.write('habab/product-tunnel/web_tunnel.py', 'changed but outside runtime package\n')
        self.write('.github/workflows/habab-android-apk-candidate.yaml', 'name: candidate\n')
        self.commit('native candidate changes')
        paths = prepare.verify_reuse_range(self.repo, self.base, self.git('rev-parse', 'HEAD'))
        self.assertIn('habab/android/app/src/main/java/App.java', paths)

    def test_reviewed_windows_inputs_and_dockerfile_copy_are_safe_if_from_is_stable(self):
        self.write('habab/distribution/start-windows.ps1', 'bootstrap change\n')
        self.write('habab/distribution/src/habab_product_tunnel/config_flow.py', 'integration\n')
        self.write('habab/distribution/Dockerfile',
                   'FROM ghcr.io/example/helper@sha256:abc\nCOPY product-tunnel/web_tunnel.py /app/\n')
        self.commit('Windows-only integration')
        revision = self.git('rev-parse', 'HEAD')
        prepare.verify_reuse_range(self.repo, self.base, revision, self.signed_runtime_expectations())

        temp_repo = Path(self.temp.name) / 'dockerfile-case'
        subprocess.run(['git', 'clone', '-q', str(self.repo), str(temp_repo)], check=True)
        self.configure_git_identity(temp_repo)
        base = subprocess.run(['git', '-C', str(temp_repo), 'rev-parse', 'HEAD'],
                              check=True, capture_output=True, text=True).stdout.strip()
        path = temp_repo / 'habab/distribution/Dockerfile'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('FROM ghcr.io/example/changed-helper@sha256:ghi\n')
        subprocess.run(['git', '-C', str(temp_repo), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(temp_repo), 'commit', '-qm', 'Dockerfile change'], check=True)
        revision = subprocess.run(['git', '-C', str(temp_repo), 'rev-parse', 'HEAD'],
                                  check=True, capture_output=True, text=True).stdout.strip()
        with self.assertRaisesRegex(ValueError, 'use a full runtime release'):
            prepare.verify_reuse_range(temp_repo, base, revision, self.signed_runtime_expectations())

    def test_linux_build_script_and_specific_ha_test_are_safe(self):
        self.write('habab/distribution/build.sh', '# linux builder update\n')
        self.write('habab/distribution/tests/test_habab_product_tunnel_ha.py', '# HA integration test\n')
        self.commit('Linux build and HA integration test')
        revision = self.git('rev-parse', 'HEAD')
        prepare.verify_reuse_range(self.repo, self.base, revision)

    def test_apk_preparation_requires_exact_clean_git_head(self):
        prepare.verify_clean_apk_checkout(self.repo, self.base)
        with self.assertRaisesRegex(ValueError, 'does not match'):
            prepare.verify_clean_apk_checkout(self.repo, 'b' * 40)
        (self.repo / 'README.md').write_text('dirty tracked source\n')
        with self.assertRaisesRegex(ValueError, 'checkout is dirty'):
            prepare.verify_clean_apk_checkout(self.repo, self.base)

    def test_android_runtime_producer_excludes_windows_only_integration_tree(self):
        producer = runpy.run_path(str(ROOT / 'habab/android/tools/package_runtime.py'))
        with tempfile.TemporaryDirectory() as temporary:
            inventory = producer['copy_sources'](Path(temporary) / 'rootfs')
        self.assertFalse(any(path.startswith('src/habab_product_tunnel/') for path in inventory))
        self.assertFalse(any('/tests/' in path or path.startswith('tests/') for path in inventory))
        self.assertNotIn('build.sh', inventory)

    def test_frontend_distribution_and_unknown_edits_require_full_runtime(self):
        for path in ('habab/native-frontend/src/app.ts', 'habab/distribution/src/habab_home/agent.py',
                     'habab/android/tools/unexpected.py'):
            with self.subTest(path=path):
                temp_repo = Path(self.temp.name) / ('case-' + str(len(path)))
                subprocess.run(['git', 'clone', '-q', str(self.repo), str(temp_repo)], check=True)
                self.configure_git_identity(temp_repo)
                prepare_rev = subprocess.run(['git', '-C', str(temp_repo), 'rev-parse', 'HEAD'],
                                             check=True, capture_output=True, text=True).stdout.strip()
                target = temp_repo / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('modified\n')
                subprocess.run(['git', '-C', str(temp_repo), 'add', '.'], check=True)
                subprocess.run(['git', '-C', str(temp_repo), 'commit', '-qm', 'blocked input'], check=True)
                revision = subprocess.run(['git', '-C', str(temp_repo), 'rev-parse', 'HEAD'],
                                          check=True, capture_output=True, text=True).stdout.strip()
                with self.assertRaisesRegex(ValueError, 'use a full runtime release'):
                    prepare.verify_reuse_range(temp_repo, prepare_rev, revision)

    def test_tampered_signed_manifest_is_rejected(self):
        manifest = Path(self.temp.name) / 'release.json'
        signature = Path(self.temp.name) / 'release.json.sig'
        private = Path(self.temp.name) / 'private.pem'
        public_der = Path(self.temp.name) / 'public.der'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048',
                        '-out', str(private)], check=True, capture_output=True)
        subprocess.run(['openssl', 'pkey', '-in', str(private), '-pubout', '-outform', 'DER',
                        '-out', str(public_der)], check=True, capture_output=True)
        manifest.write_text('{"sourceRevision":"a"}')
        raw_signature = Path(self.temp.name) / 'sig.bin'
        subprocess.run(['openssl', 'dgst', '-sha256', '-sign', str(private), '-out', str(raw_signature),
                        str(manifest)], check=True, capture_output=True)
        import base64
        signature.write_bytes(base64.b64encode(raw_signature.read_bytes()))
        prepare.verify_signature(manifest, signature, public_der)
        manifest.write_text('{"sourceRevision":"b"}')
        with self.assertRaises(subprocess.CalledProcessError):
            prepare.verify_signature(manifest, signature, public_der)

    def test_apk_and_runtime_source_revisions_are_separate(self):
        release = {'sourceRevision': self.base, 'id': 'runtime123', 'nativeFrontend': {'version': '20'},
                   'url': 'https://habab-demo.wpmix.net/downloads/android/habab-runtime-runtime123-arm64.tgz'}
        manifest = Path(self.temp.name) / 'release.json'
        manifest.write_text(json.dumps(release))
        apk_revision = 'b' * 40
        apk = Path(self.temp.name) / 'app.apk'
        with zipfile.ZipFile(apk, 'w') as archive:
            archive.writestr('assets/release/build-provenance.json', json.dumps(
                verify_runtime.apk_provenance(release, manifest, apk_revision)))
            archive.writestr('assets/release/default-url.txt', verify_runtime.runtime_manifest_url(release))
        verify_runtime.verify_apk_provenance(apk, release, manifest, apk_revision)
        self.assertEqual(verify_runtime.apk_provenance(release, manifest)['sourceRevision'], self.base)
        self.assertNotIn('apkSourceRevision', verify_runtime.apk_provenance(release, manifest))


if __name__ == '__main__':
    unittest.main()
