import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from publish_runtime import publish
from verify_runtime import decode_signature, validate_source_revision


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.destination = self.root / 'public'
        self.identity = 'a' * 16
        self.source_revision = 'd' * 40
        self.archive = self.source / f'habab-runtime-{self.identity}-arm64.tgz'
        self.archive.write_bytes(b'fixture')
        self.manifest = self.source / f'release-{self.identity}.json'
        self.manifest.write_text(json.dumps({'id': self.identity, 'url': 'https://example.test/' + self.archive.name,
                                            'sha256': hashlib.sha256(b'fixture').hexdigest(),
                                            'sourceRevision': self.source_revision}))
        Path(str(self.manifest) + '.sig').write_text('fixture')
        self.state = self.root / 'state.json'
        current = 'b' * 16
        previous = 'c' * 16
        old_current = self.root / 'old-current'
        old_previous = self.root / 'old-previous'
        old_current.write_text('current')
        old_previous.write_text('previous')
        self.state.write_text(json.dumps({
            'schema': 1,
            'root': str(self.root),
            'current': current,
            'previous': previous,
            'releases': {current: ['old-current'], previous: ['old-previous']},
        }))
        self.tool = self.root / 'retention.py'
        self.tool.write_text('# fixture')

    def publish_args(self):
        return (self.manifest, self.root / 'key', self.destination, self.state, self.tool, self.source_revision)

    def preview(self, command, **kwargs):
        self.assertNotIn('--release', command)
        self.assertNotIn('--apply', command)
        return subprocess.CompletedProcess(command, 0, json.dumps({'status': 'PREVIEW'}), '')

    def test_verification_failure_never_publishes_or_prunes(self):
        before = self.state.read_bytes()
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            if '--release' in command:
                self.fail('retention apply ran after a verification failure')
            return self.preview(command, **kwargs)
        with patch('publish_runtime.verify', side_effect=ValueError('bad signature')), patch('publish_runtime.subprocess.run', side_effect=run):
            with self.assertRaises(ValueError):
                publish(*self.publish_args())
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(sum('--release' in command for command in commands), 0)

    def test_cleanup_runs_only_after_the_complete_bundle_exists(self):
        commands = []
        def observe(command, **kwargs):
            commands.append(command)
            if '--release' not in command:
                return self.preview(command, **kwargs)
            self.assertIn('--apply', command)
            self.assertEqual(len(list(self.destination.iterdir())), 3)
            self.assertIn(self.identity, command)
            return subprocess.CompletedProcess(command, 0, '{"status":"APPLY"}', '')
        with patch('publish_runtime.verify'), patch('publish_runtime.subprocess.run', side_effect=observe):
            publish(*self.publish_args())
        self.assertEqual(sum('--release' in command for command in commands), 1)
        self.assertEqual(sum('--release' not in command for command in commands), 1)

    def test_existing_immutable_file_is_not_overwritten(self):
        self.destination.mkdir()
        target = self.destination / self.archive.name
        target.write_bytes(b'different')
        def run(command, **kwargs):
            if '--release' in command:
                self.fail('retention apply ran after immutable artifact conflict')
            return self.preview(command, **kwargs)
        with patch('publish_runtime.verify'), patch('publish_runtime.subprocess.run', side_effect=run):
            with self.assertRaises(ValueError):
                publish(*self.publish_args())
        self.assertEqual(target.read_bytes(), b'different')

    def test_wrong_expected_source_revision_fails_before_any_write(self):
        with patch('publish_runtime.verify') as verify, patch('publish_runtime.subprocess.run') as retention:
            with self.assertRaisesRegex(ValueError, 'sourceRevision'):
                publish(self.manifest, self.root / 'key', self.destination, self.state, self.tool, 'e' * 40)
        self.assertFalse(self.destination.exists())
        verify.assert_not_called()
        retention.assert_not_called()

    def test_retention_preview_rejects_uninitialized_state_before_publish(self):
        def skip(command, **kwargs):
            self.assertNotIn('--release', command)
            return subprocess.CompletedProcess(command, 0, json.dumps({'status': 'SKIP', 'reason': 'uninitialized'}), '')
        with patch('publish_runtime.verify') as verify, patch('publish_runtime.subprocess.run', side_effect=skip):
            with self.assertRaisesRegex(ValueError, 'retention state is not ready'):
                publish(*self.publish_args())
        self.assertFalse(self.destination.exists())
        verify.assert_not_called()

    def test_signature_decoder_accepts_outer_newline_but_rejects_junk(self):
        encoded = b'c2lnbmF0dXJl'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'release.sig'
            path.write_bytes(encoded + b'\n')
            self.assertEqual(decode_signature(path), b'signature')
            path.write_bytes(encoded + b'!')
            with self.assertRaisesRegex(ValueError, 'Malformed release signature'):
                decode_signature(path)

    def test_embedded_source_revision_must_match_outer_and_expected(self):
        outer = {'sourceRevision': self.source_revision}
        embedded = {'sourceRevision': self.source_revision}
        validate_source_revision(outer, embedded, self.source_revision)
        with self.assertRaisesRegex(ValueError, 'Embedded runtime sourceRevision'):
            validate_source_revision(outer, {'sourceRevision': 'e' * 40}, self.source_revision)


if __name__ == '__main__':
    unittest.main()
