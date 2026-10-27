"""ARM64 installer selection/integrity checks; no Docker, network or binary execution."""
import base64
import contextlib
import hashlib
import io
import json
from pathlib import Path
import platform
import tarfile
import tempfile
import unittest
from unittest.mock import patch


DIST = Path(__file__).resolve().parents[1]


class Arm64OpenCodeBootstrapTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='habab-arm64-opencode-fixture-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.target = self.root / 'volume/opencode'
        self.stamp = self.target.parent / 'release.json'
        # This is deliberately synthetic, never executed as an ELF program.
        self.binary = b'synthetic-arm64-musl-executable'
        package = io.BytesIO()
        with tarfile.open(fileobj=package, mode='w:gz') as archive:
            entry = tarfile.TarInfo('package/bin/opencode')
            entry.size = len(self.binary)
            archive.addfile(entry, io.BytesIO(self.binary))
        self.archive = package.getvalue()
        self.release = json.loads((DIST / 'opencode-release.json').read_text())
        self.arm_url = self.release['arm64']['url']
        self.release['arm64']['integrity'] = 'sha512-' + base64.b64encode(
            hashlib.sha512(self.archive).digest()).decode()
        self.pin = self.root / 'opencode-release.json'
        self.pin.write_text(json.dumps(self.release))
        source = (DIST / 'install-opencode-volume.py').read_text()
        # Redirect the two filesystem roots, then execute the actual installer.
        self.assertEqual(source.count("Path('/bootstrap/opencode-release.json')"), 1)
        self.assertEqual(source.count("Path('/opt/habab-opencode/opencode')"), 1)
        self.source = source.replace("Path('/bootstrap/opencode-release.json')", f'Path({str(self.pin)!r})')
        self.source = self.source.replace("Path('/opt/habab-opencode/opencode')", f'Path({str(self.target)!r})')

    def run_installer(self, response=None):
        with patch.object(platform, 'machine', return_value='aarch64'), \
                patch('urllib.request.urlopen', return_value=io.BytesIO(
                    self.archive if response is None else response)) as request, \
                contextlib.redirect_stdout(io.StringIO()):
            exec(compile(self.source, str(DIST / 'install-opencode-volume.py'), 'exec'), {})
        return request

    def seed_cache(self, architecture, binary):
        self.target.parent.mkdir()
        self.target.write_bytes(binary)
        self.stamp.write_text(json.dumps({'release': self.release[architecture],
            'sha256': hashlib.sha256(binary).hexdigest()}))

    def test_aarch64_selects_arm64_musl_pin_and_records_actual_digest(self):
        request = self.run_installer()
        request.assert_called_once_with(self.arm_url, timeout=90)
        self.assertEqual(self.target.read_bytes(), self.binary)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o755)
        saved = json.loads(self.stamp.read_text())
        self.assertEqual(saved['release'], self.release['arm64'])
        self.assertEqual(saved['sha256'], hashlib.sha256(self.binary).hexdigest())

    def test_matching_arm64_cache_does_not_download_again(self):
        self.seed_cache('arm64', self.binary)
        self.run_installer().assert_not_called()
        self.assertEqual(self.target.read_bytes(), self.binary)

    def test_existing_amd64_cache_is_not_reused_on_arm64(self):
        self.seed_cache('amd64', b'synthetic-x64-executable')
        self.run_installer().assert_called_once_with(self.arm_url, timeout=90)
        self.assertEqual(self.target.read_bytes(), self.binary)
        self.assertEqual(json.loads(self.stamp.read_text())['release'], self.release['arm64'])

    def test_corrupt_receipt_recovers_from_pinned_arm64_archive(self):
        self.seed_cache('arm64', b'cached-executable')
        self.stamp.write_text('{truncated')

        request = self.run_installer()

        request.assert_called_once_with(self.arm_url, timeout=90)
        saved = json.loads(self.stamp.read_text())
        self.assertEqual(saved['release'], self.release['arm64'])
        self.assertEqual(saved['sha256'], hashlib.sha256(self.binary).hexdigest())
        self.assertEqual(self.target.read_bytes(), self.binary)
        self.assertFalse(self.stamp.with_suffix('.next').exists())

    def test_corrupt_download_does_not_replace_existing_executable_or_stamp(self):
        self.seed_cache('amd64', b'keep-existing')
        before = self.stamp.read_bytes()
        with self.assertRaisesRegex(SystemExit, 'integrity check failed'):
            self.run_installer(response=b'corrupt archive')
        self.assertEqual(self.target.read_bytes(), b'keep-existing')
        self.assertEqual(self.stamp.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
