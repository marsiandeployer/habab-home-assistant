import base64
from io import BytesIO
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

from verify_runtime import verify


class SerialxUnixReleaseVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.private_key = self.directory / 'private.pem'
        self.public_key = self.directory / 'public.der'
        subprocess.run(
            [
                'openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt',
                'rsa_keygen_bits:2048', '-out', str(self.private_key),
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                'openssl', 'pkey', '-in', str(self.private_key), '-pubout',
                '-outform', 'DER', '-out', str(self.public_key),
            ],
            check=True,
            capture_output=True,
        )

    def make_release(self, *, wrong_serialx_hash=False):
        site = 'usr/local/lib/python3.14/site-packages'
        init_path = f'{site}/serialx/platforms/__init__.py'
        unix_path = f'{site}/serialx/platforms/serial_unix.py'
        metadata_path = f'{site}/serialx-1.11.0.dist-info/METADATA'
        init_bytes = b'from . import serial_unix\n'
        unix_bytes = b'def transport_marker():\n    return "unix"\n'
        metadata_bytes = b'Metadata-Version: 2.1\nName: serialx\nVersion: 1.11.0\n'
        attestation = {
            'packageVersion': '1.11.0',
            'pythonSitePackages': site,
            'files': {
                init_path: hashlib.sha256(init_bytes).hexdigest(),
                unix_path: hashlib.sha256(unix_bytes).hexdigest(),
            },
        }
        if wrong_serialx_hash:
            attestation['files'][unix_path] = '0' * 64

        contents = {
            'usr/local/bin/python3': b'python',
            'usr/local/bin/habab-ha': b'habab-ha',
            'usr/local/bin/habab-install-xiaomi-home': b'install',
            'usr/bin/node': b'node',
            'usr/src/homeassistant/homeassistant/components/habab_home/__init__.py': b'',
            'usr/src/homeassistant/homeassistant/components/habab_home/frontend/setup-chat.js': b'',
            'opt/habab-opencode/opencode': b'opencode',
            'usr/local/share/habab-android/launch.py': b'launcher',
            init_path: init_bytes,
            unix_path: unix_bytes,
            metadata_path: metadata_bytes,
            'etc/habab-android-release.json': json.dumps(
                {'serialxUnixTransport': attestation}, sort_keys=True
            ).encode(),
        }
        archive = self.directory / 'runtime.tgz'
        with tarfile.open(archive, 'w:gz') as bundle:
            for name, data in contents.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                bundle.addfile(member, BytesIO(data))

        manifest = self.directory / 'release.json'
        release = {
            'schema': 1,
            'id': 'test-release',
            'url': 'https://example.test/runtime.tgz',
            'bytes': archive.stat().st_size,
            'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
            'unpackedBytes': sum(map(len, contents.values())),
            'opencodeSha256': hashlib.sha256(b'opencode').hexdigest(),
            'androidLauncherSha256': hashlib.sha256(b'launcher').hexdigest(),
            'sourceFiles': {},
            'serialxUnixTransport': attestation,
        }
        manifest.write_text(json.dumps(release, sort_keys=True))
        raw_signature = self.directory / 'signature.bin'
        subprocess.run(
            [
                'openssl', 'dgst', '-sha256', '-sign', str(self.private_key),
                '-out', str(raw_signature), str(manifest),
            ],
            check=True,
            capture_output=True,
        )
        Path(str(manifest) + '.sig').write_text(
            base64.b64encode(raw_signature.read_bytes()).decode('ascii')
        )
        return manifest

    def test_strict_mode_rejects_release_without_serialx_attestation(self):
        manifest = self.directory / 'legacy.json'
        manifest.write_text('{}')

        with self.assertRaisesRegex(ValueError, 'does not attest'):
            verify(manifest, self.public_key, require_serialx_unix=True)

    def test_signed_archive_accepts_matching_attestation(self):
        manifest = self.make_release()

        verify(manifest, self.public_key, require_serialx_unix=True)

    def test_signed_archive_rejects_changed_payload_hash(self):
        manifest = self.make_release(wrong_serialx_hash=True)

        with self.assertRaisesRegex(ValueError, 'payload mismatch'):
            verify(manifest, self.public_key, require_serialx_unix=True)


if __name__ == '__main__':
    unittest.main()
