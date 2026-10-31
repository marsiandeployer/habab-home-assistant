import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


DISTRIBUTION = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DISTRIBUTION))
spec = importlib.util.spec_from_file_location('package_windows', DISTRIBUTION / 'package-windows.py')
packager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packager)


class WindowsInstallerTests(unittest.TestCase):
    def test_payload_records_source_and_github_asset_url_without_private_files(self):
        source = 'a' * 40
        base = 'https://github.com/example/habab/releases/download/v0.3.6'
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with patch.object(packager, 'resolve_source_provenance', return_value=(source, 'false')):
                with contextlib.redirect_stdout(io.StringIO()):
                    metadata = packager.package_windows(DISTRIBUTION, output, base)
            archive_bytes = (output / metadata['filename']).read_bytes()
            self.assertEqual(hashlib.sha256(archive_bytes).hexdigest(), metadata['sha256'])
            self.assertEqual(metadata['source_revision'], source)
            self.assertEqual(metadata['source_dirty'], 'false')
            self.assertNotIn('installer', metadata, 'An unbuilt EXE must not be advertised')
            launcher = (output / 'start-home-assistant.bat').read_text()
            self.assertIn(base + '/' + metadata['filename'], launcher)
            self.assertIn(metadata['sha256'], launcher)
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                names = archive.namelist()
                self.assertEqual(names, metadata['files'])
                for name in names:
                    self.assertNotIn('..', Path(name).parts)
                    self.assertFalse(any(part.startswith('.env') for part in Path(name).parts), name)
                    self.assertFalse({'secrets', '.storage', 'qa', 'tests', '__pycache__'} & set(Path(name).parts), name)
                    self.assertNotIn(Path(name).suffix, {'.key', '.pem', '.wav', '.mp3', '.onnx', '.pt'})
                embedded = json.loads(archive.read('release.json'))
                self.assertEqual(embedded['source_revision'], source)
                self.assertEqual(embedded['source_dirty'], 'false')
                self.assertIn(b'\r\n', archive.read('start-windows.bat'))
                self.assertEqual(archive.read('start-windows.bat'),
                                 (DISTRIBUTION / 'start-windows.bat').read_bytes())

    def test_public_base_rejects_powershell_and_cmd_injection(self):
        for value in (
            'http://github.com/x', 'https://user:password@github.com/x',
            'https://github.com/x?token=private', 'https://github.com/x#fragment',
            "https://github.com/x';Write-Host hacked;#", 'https://github.com/%PATH%/x',
            'https://github.com/x\n', 'https://github.com/x"',
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                packager.public_base_url(value)

    def test_installer_is_recorded_only_after_compilation_from_clean_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            metadata_path = output / 'windows-package.json'
            metadata = {'source_revision': 'b' * 40, 'source_dirty': 'false'}
            metadata_path.write_text(json.dumps(metadata))
            executable = b'MZ' + b'test executable fixture'
            (output / packager.INSTALLER_FILENAME).write_bytes(executable)
            result = packager.record_installer(output, '6.7.1')
            self.assertEqual(result['filename'], 'habab_home_assistant.exe')
            self.assertEqual(result['sha256'], hashlib.sha256(executable).hexdigest())
            self.assertEqual(result['bytes'], len(executable))
            self.assertEqual(result['source_revision'], 'b' * 40)
            self.assertIs(result['source_dirty'], False)
            metadata['source_dirty'] = 'true'
            metadata_path.write_text(json.dumps(metadata))
            with self.assertRaisesRegex(ValueError, 'clean source'):
                packager.record_installer(output, '6.7.1')
            metadata['source_dirty'] = 'false'
            metadata_path.write_text(json.dumps(metadata))
            (output / packager.INSTALLER_FILENAME).write_bytes(b'not an exe')
            with self.assertRaisesRegex(ValueError, 'PE executable'):
                packager.record_installer(output, '6.7.1')


if __name__ == '__main__':
    unittest.main()
