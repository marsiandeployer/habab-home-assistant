import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile

import test_release_candidate as fixtures
from test_nested_oci import make_oci

SPEC = importlib.util.spec_from_file_location('github_release', Path(__file__).with_name('github-release.py'))
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
REPO = Path(__file__).resolve().parents[2]


class GithubReleaseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ReleaseCandidateTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.revision = 'e' * 40
        self.fixture.workflow_identity['workflow_revision'] = self.fixture.revision
        self.root = self.fixture.root
        self.source = self.fixture.source_tree
        self.revision = self.fixture.revision
        self.tag = 'habab-v0.3.6-' + self.revision[:12]
        self.base = release.release_base('org/repo', self.tag, self.revision)
        self.output = self.root.parent / 'release'

    def populate(self):
        self.fixture.add_windows_package()
        exe = self.root / 'windows/habab_home_assistant.exe'
        exe.write_bytes(b'MZ' + b'synthetic PE fixture')
        meta_path = self.root / 'windows/windows-package.json'
        meta = json.loads(meta_path.read_text())
        sha, size = release.digest(exe)
        meta['installer'] = {'filename': exe.name, 'sha256': sha, 'bytes': size,
                             'source_revision': self.revision, 'source_dirty': False}
        meta_path.write_text(json.dumps(meta))
        for arch in ('amd64', 'arm64'):
            self.fixture.add_status('linux-' + arch)
            image = self.root / f'linux-{arch}/habab-home-linux-{arch}.oci.tar.gz'
            make_oci(image)
            import gzip
            image.write_bytes(gzip.compress(image.read_bytes(), mtime=0))
            path = image.parent / 'status.json'
            status = json.loads(path.read_text())
            status['oci_image'] = release.candidate.verify_linux_image(image, f'linux/{arch}', self.revision)
            path.write_text(json.dumps(status))
        self.fixture.add_status('android', apk_signature='verified-release', publishable=True,
                                runtime_manifest=self.base + '/release-test.json')
        android = self.root / 'android'
        archive = android / 'runtime-test.tgz'
        archive.write_bytes(b'synthetic signed archive')
        sha, size = release.digest(archive)
        frontend_pin = {'sha256': 'b' * 64, 'producer_source_commit': 'd' * 40}
        (self.source / 'habab/distribution/native-frontend-release.json').write_text(json.dumps(frontend_pin))
        runtime = {'id': 'test', 'sourceRevision': self.revision,
                   'nativeFrontend': {'sourceRevision': self.revision, 'wheelSha256': frontend_pin['sha256']},
                   'url': self.base + '/' + archive.name, 'sha256': sha, 'bytes': size}
        manifest = android / 'release-test.json'
        manifest.write_text(json.dumps(runtime))
        tools = self.source / 'habab/android/tools'
        tools.mkdir(parents=True)
        shutil.copyfile(REPO / 'habab/android/tools/verify_runtime.py', tools / 'verify_runtime.py')
        key = self.root.parent / 'test-key.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:1024', '-out', str(key)],
                       check=True, capture_output=True)
        pub = self.source / 'habab/android/app/src/main/assets/release/trusted-key.der'
        pub.parent.mkdir(parents=True)
        subprocess.run(['openssl', 'pkey', '-in', str(key), '-pubout', '-outform', 'DER', '-out', str(pub)],
                       check=True, capture_output=True)
        import base64
        sig = subprocess.check_output(['openssl', 'dgst', '-sha256', '-sign', str(key), str(manifest)])
        Path(str(manifest) + '.sig').write_bytes(base64.b64encode(sig))
        import hashlib
        provenance = {'sourceRevision': self.revision, 'runtimeId': 'test',
                      'runtimeManifestSha256': hashlib.sha256(manifest.read_bytes()).hexdigest(),
                      'runtimeManifestUrl': self.base + '/' + manifest.name, 'nativeFrontend': runtime['nativeFrontend']}
        with zipfile.ZipFile(android / 'habab-home-arm64.apk', 'w') as apk:
            apk.writestr('assets/release/build-provenance.json', json.dumps(provenance))
            apk.writestr('assets/release/default-url.txt', self.base + '/' + manifest.name + '\n')
        self.record_manifest()

    def record_manifest(self):
        manifest = release.candidate.assemble(self.root, self.revision, 'built', self.source, 'all', self.fixture.workflow_identity)
        manifest.update(self.fixture.workflow_identity)
        (self.root / 'release-candidate.json').write_text(json.dumps(manifest))

    def stage(self):
        return release.stage(self.root, self.source, self.revision, 'org/repo', self.tag, self.output)

    def test_complete_signed_candidate_stages_only_public_payloads_and_checksums(self):
        self.populate()
        receipt = self.stage()
        names = {item['name'] for item in receipt['assets']}
        self.assertIn('habab_home_assistant.exe', names)
        self.assertIn('habab-home-arm64.apk', names)
        self.assertIn('habab-home-linux-amd64.oci.tar.gz', names)
        self.assertIn('habab-home-linux-arm64.oci.tar.gz', names)
        self.assertNotIn('artifact.bin', names)
        self.assertEqual(receipt['windows_linux_frontend_dependency']['producer_source_commit'], 'd' * 40)
        self.assertEqual(receipt['source_revision'], self.revision)
        subprocess.run(['sha256sum', '--check', 'SHA256SUMS'], cwd=self.output, check=True, capture_output=True)
        remote = {'assets': [{'name': item['name'], 'size': item['bytes'], 'digest': 'sha256:' + item['sha256'],
                              'state': 'uploaded'} for item in receipt['assets']]}
        release.verify_uploaded(receipt, remote)

    def test_blocks_missing_or_blocked_platform_even_if_manifest_claims_success(self):
        self.populate()
        path = self.root / 'release-candidate.json'
        manifest = json.loads(path.read_text())
        manifest['targets']['android']['state'] = 'blocked'
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'blocked, skipped or unsigned'):
            self.stage()

    def test_rejects_different_frontend_wheel_content_between_platforms(self):
        self.populate()
        path = self.source / 'habab/distribution/native-frontend-release.json'
        pin = json.loads(path.read_text())
        pin['sha256'] = 'a' * 64
        path.write_text(json.dumps(pin))
        with self.assertRaisesRegex(ValueError, 'frontend wheel SHA-256 differs'):
            self.stage()
        self.assertFalse(self.output.exists(), 'Mismatch must fail before staging/publishing assets')

    def test_rejects_changed_bytes_after_candidate_checksums(self):
        self.populate()
        (self.root / 'windows/habab_home_assistant.exe').write_bytes(b'MZtampered')
        with self.assertRaisesRegex(ValueError, 'changed after manifest'):
            self.stage()

    def test_rejects_unsigned_runtime_even_with_rewritten_candidate_manifest(self):
        self.populate()
        (self.root / 'android/release-test.json.sig').write_bytes(b'ZmFrZQ==')
        self.record_manifest()
        with self.assertRaises(subprocess.CalledProcessError):
            self.stage()

    def test_rejects_private_models_even_with_valid_candidate_checksums(self):
        self.populate()
        with zipfile.ZipFile(self.root / 'android/habab-home-arm64.apk', 'a') as apk:
            apk.writestr('assets/voice/experiments/microwakeword/classifier.tflite', b'private')
        self.record_manifest()
        with self.assertRaisesRegex(ValueError, 'private experimental'):
            self.stage()

    def test_rejects_remote_corruption_missing_duplicate_or_unexpected_assets(self):
        receipt = {'assets': [{'name': 'app.exe', 'bytes': 42, 'sha256': 'a' * 64}]}
        remote = {'assets': [{'name': 'app.exe', 'size': 42, 'digest': 'sha256:' + 'a' * 64, 'state': 'uploaded'}]}
        for field, value in [('size', 41), ('digest', 'sha256:' + 'b' * 64), ('state', 'new')]:
            bad = copy.deepcopy(remote)
            bad['assets'][0][field] = value
            with self.assertRaisesRegex(ValueError, 'checksum/size mismatch'):
                release.verify_uploaded(receipt, bad)
        for assets in ([], remote['assets'] * 2, remote['assets'] + [{'name': 'unreviewed'}]):
            with self.assertRaisesRegex(ValueError, 'inventory differs'):
                release.verify_uploaded(receipt, {'assets': assets})

    def test_distinct_controller_sha_preserves_the_frozen_product_identity(self):
        self.populate()
        controller = 'f' * 40
        self.fixture.workflow_identity['workflow_revision'] = controller
        for target in release.TARGETS:
            path = self.root / target / 'status.json'
            status = json.loads(path.read_text())
            status['workflow_revision'] = controller
            path.write_text(json.dumps(status))
        self.record_manifest()
        receipt = release.stage(self.root, self.source, self.revision, 'org/repo', self.tag, self.output, controller)
        self.assertEqual(receipt['source_revision'], self.revision)
        self.assertEqual(receipt['workflow_revision'], controller)
        self.assertTrue(all(status['source_revision'] == self.revision for status in receipt['targets'].values()))

    def test_controller_range_rejects_intermediate_product_change_even_when_reverted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def git(*arguments):
                return subprocess.check_output(['git', '-C', str(root), *arguments], stderr=subprocess.DEVNULL).decode().strip()
            git('init', '-b', 'main')
            git('config', 'user.name', 'CI fixture')
            git('config', 'user.email', 'ci@example.invalid')
            product = root / 'habab/distribution/product.py'
            product.parent.mkdir(parents=True)
            product.write_text('frozen product\n')
            git('add', '.')
            git('commit', '-qm', 'Frozen product')
            frozen = git('rev-parse', 'HEAD')
            workflow = root / '.github/workflows/habab-release.yaml'
            workflow.parent.mkdir(parents=True)
            workflow.write_text('reviewed CI correction\n')
            git('add', '.')
            git('commit', '-qm', 'CI only')
            controller = git('rev-parse', 'HEAD')
            release.validate_controller_range(root, frozen, controller)
            product.write_text('changed product\n')
            git('commit', '-qam', 'Product changed')
            product.write_text('frozen product\n')
            git('commit', '-qam', 'Product reverted')
            with self.assertRaisesRegex(ValueError, 'complete controller range'):
                release.validate_controller_range(root, frozen, git('rev-parse', 'HEAD'))

    def test_frontend_reuse_requires_completed_trusted_run_at_exact_product_sha(self):
        producer = {'head_sha': self.revision, 'status': 'completed', 'event': 'workflow_dispatch',
                    'repository': {'full_name': 'org/repo'}, 'path': '.github/workflows/habab-release.yaml',
                    'conclusion': 'failure'}  # Another platform may fail after the frontend artifact is complete.
        release.validate_frontend_run(producer, 'org/repo', self.revision)
        for field, value in (('head_sha', 'a' * 40), ('status', 'in_progress'), ('event', 'pull_request'),
                             ('repository', {'full_name': 'other/repo'}), ('path', '.github/workflows/untrusted.yaml')):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'exact product SHA'):
                release.validate_frontend_run({**producer, field: value}, 'org/repo', self.revision)

    def test_whole_candidate_reuse_checks_api_run_attempt_jobs_and_apk_version(self):
        self.populate()
        path = self.root / 'android/status.json'
        status = json.loads(path.read_text())
        status['apk_version'] = {'version_code': 58, 'version_name': '0.1.58'}
        path.write_text(json.dumps(status))
        self.record_manifest()
        manifest = json.loads((self.root / 'release-candidate.json').read_text())
        run = {'id': 12345, 'run_attempt': 1, 'head_sha': self.revision, 'head_branch': 'main',
               'status': 'completed', 'event': 'workflow_dispatch', 'repository': {'full_name': 'org/repo'},
               'path': '.github/workflows/habab-release.yaml'}
        names = ['candidate / windows-source', 'candidate / android', 'candidate / assemble-candidate',
                 'candidate / linux-image (linux/amd64, linux-amd64)',
                 'candidate / linux-image (linux/arm64, linux-arm64)']
        jobs = [{'name': name, 'conclusion': 'success'} for name in names]
        identity = release.validate_candidate_reuse(run, manifest, jobs, 'org/repo', self.revision, '12345', '58', '0.1.58')
        self.assertEqual(identity, self.fixture.workflow_identity)
        for bad_run, bad_manifest, bad_jobs, version in (
            ({**run, 'id': 999}, manifest, jobs, '58'),
            ({**run, 'run_attempt': 2}, manifest, jobs, '58'),
            ({**run, 'head_sha': 'a' * 40}, manifest, jobs, '58'),
            (run, manifest, jobs[:-1], '58'),
            (run, manifest, jobs, '59'),
        ):
            with self.assertRaises(ValueError):
                release.validate_candidate_reuse(bad_run, bad_manifest, bad_jobs, 'org/repo', self.revision, '12345', version, '0.1.58')
        changed = copy.deepcopy(manifest)
        changed['targets']['linux-amd64']['workflow_run_id'] = '999'
        with self.assertRaisesRegex(ValueError, 'producer identity'):
            release.validate_candidate_reuse(run, changed, jobs, 'org/repo', self.revision, '12345', '58', '0.1.58')

    def test_publish_only_keeps_original_producer_and_separate_publisher_revision(self):
        self.populate()
        receipt = release.stage(self.root, self.source, self.revision, 'org/repo', self.tag, self.output,
                                self.revision, 'f' * 40, '12345', '1')
        self.assertEqual(receipt['workflow_revision'], self.revision)
        self.assertEqual(receipt['workflow_run_id'], '12345')
        self.assertEqual(receipt['workflow_run_attempt'], '1')
        self.assertEqual(receipt['publisher_workflow_revision'], 'f' * 40)
        self.assertTrue(all(item['workflow_run_id'] == '12345' for item in receipt['targets'].values()))
        with self.assertRaisesRegex(ValueError, 'expected producer run/attempt'):
            release.stage(self.root, self.source, self.revision, 'org/repo', self.tag, self.output,
                          self.revision, 'f' * 40, '12345', '2')

    def test_publish_only_workflow_has_windows_sources_and_skipped_candidate_guard(self):
        workflow = (REPO / '.github/workflows/habab-release.yaml').read_text()
        product_checkout = workflow[workflow.index('      - name: Check out the frozen product separately'):]
        product_checkout = product_checkout[:product_checkout.index('      - uses: actions/download-artifact')]
        self.assertIn('habab/product-tunnel', product_checkout)
        publish = workflow[workflow.index('  publish:\n'):]
        self.assertIn("always() && needs.validate.result == 'success'", publish)
        self.assertIn("needs.validate.outputs.reuse_candidate == 'true' && needs.candidate.result == 'skipped'", publish)
        self.assertIn('actions: read', publish)
        self.assertEqual(publish.count('run-id: ${{ needs.validate.outputs.producer_run_id }}'), 2)
        self.assertEqual(publish.count('github-token: ${{ github.token }}'), 2)
        self.assertIn('--publisher-revision "$WORKFLOW_REVISION"', publish)
        self.assertIn('--workflow-revision "$PRODUCER_WORKFLOW_REVISION"', publish)

    def test_release_tag_accepts_only_absent_or_exact_lightweight_source(self):
        self.assertFalse(release.validate_tag_target(None, self.revision))
        reference = {'object': {'type': 'commit', 'sha': self.revision}}
        self.assertTrue(release.validate_tag_target(reference, self.revision))
        for target in ({'type': 'commit', 'sha': 'a' * 40}, {'type': 'tag', 'sha': self.revision}, {}):
            with self.assertRaisesRegex(ValueError, 'exact product SHA'):
                release.validate_tag_target({'object': target}, self.revision)

    def test_tag_lookup_treats_only_http_404_as_absence(self):
        from unittest.mock import patch
        for status in ('404', '403'):
            response = subprocess.CompletedProcess([], 1, stdout=json.dumps({'status': status}), stderr='')
            with patch.object(release.subprocess, 'run', return_value=response):
                if status == '404':
                    self.assertIsNone(release.read_tag_reference('org/repo', self.tag))
                else:
                    with self.assertRaisesRegex(RuntimeError, 'only HTTP 404'):
                        release.read_tag_reference('org/repo', self.tag)

    def test_matching_tag_never_allows_an_existing_release_or_draft(self):
        self.assertTrue(release.validate_tag_target({'object': {'type': 'commit', 'sha': self.revision}}, self.revision))
        for draft in (False, True):
            with self.subTest(draft=draft), self.assertRaisesRegex(ValueError, 'release/draft'):
                release.reject_existing_release([{'tag_name': 'other'}, {'tag_name': self.tag, 'draft': draft}], self.tag)
        release.reject_existing_release([{'tag_name': 'other', 'draft': True}], self.tag)
        workflow = (REPO / '.github/workflows/habab-release.yaml').read_text()
        self.assertIn('gh api --paginate --slurp', workflow)
        creation = workflow[workflow.index('      - name: Create draft and upload verified assets'):]
        self.assertIn("if not helpers['validate_tag_target']", creation)
        self.assertIn("helpers['reject_existing_release']", creation)
        self.assertNotIn('--force', creation)
        self.assertNotIn('--clobber', creation)

    def test_release_tag_must_pin_exact_source_and_have_no_url_components(self):
        for tag in ('latest', self.tag + '/extra', self.tag + '?x=1', 'habab-v0.3.6-' + 'a' * 12):
            with self.assertRaises(ValueError):
                release.release_base('org/repo', tag, self.revision)


if __name__ == '__main__':
    unittest.main()
