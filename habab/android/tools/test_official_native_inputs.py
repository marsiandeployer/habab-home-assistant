"""Official native/voice downloads retain pinned content and offline checks."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import prepare_native
import prepare_voice


class OfficialNativeInputTests(unittest.TestCase):
    def test_native_uses_official_termux_filename_and_checks_downloaded_sha(self):
        payload = b'synthetic native package'
        record = {'Filename': 'pool/main/p/proot/proot_fixture_aarch64.deb',
                  'SHA256': hashlib.sha256(payload).hexdigest()}
        for content in (payload, b'corrupt native package'):
            with self.subTest(valid=content == payload), tempfile.TemporaryDirectory() as temporary:
                def download(url, path):
                    self.assertEqual(url, 'https://packages.termux.dev/apt/termux-main/' + record['Filename'])
                    path.write_bytes(content)
                with patch.object(prepare_native.urllib.request, 'urlretrieve', side_effect=download) as fetch:
                    if content == payload:
                        result = prepare_native.package_file('proot', record, Path(temporary))
                        self.assertEqual(result.read_bytes(), payload)
                    else:
                        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                            prepare_native.package_file('proot', record, Path(temporary))
                    fetch.assert_called_once()

    def test_native_does_not_fall_back_when_official_source_is_unavailable(self):
        record = {'Filename': 'pool/main/p/proot/missing.deb', 'SHA256': 'a' * 64}
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(prepare_native.urllib.request, 'urlretrieve', side_effect=OSError('404')) as fetch:
            with self.assertRaises(OSError):
                prepare_native.package_file('proot', record, Path(temporary))
            fetch.assert_called_once()

    def test_explicit_offline_native_cache_never_downloads_and_still_checks_sha(self):
        payload = b'synthetic cached package'
        record = {'SHA256': hashlib.sha256(payload).hexdigest()}
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(prepare_native.urllib.request, 'urlretrieve') as fetch:
            cache = Path(temporary)
            with self.assertRaises(FileNotFoundError):
                prepare_native.package_file('proot', record, cache, offline=True)
            (cache / 'proot.deb').write_bytes(payload)
            self.assertEqual(prepare_native.package_file('proot', record, cache, offline=True).read_bytes(), payload)
            (cache / 'proot.deb').write_bytes(b'corrupt cache')
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                prepare_native.package_file('proot', record, cache, offline=True)
            fetch.assert_not_called()

    def test_voice_downloads_official_manifest_url_and_checks_size_and_sha_before_copy(self):
        payload = b'synthetic voice model archive'
        official = json.loads((prepare_voice.ROOT / 'voice-model.json').read_text())
        self.assertEqual(official['url'], 'https://alphacephei.com/vosk/models/' + official['name'] + '.zip')
        for content in (payload, payload[:-1], b'x' * len(payload)):
            with self.subTest(valid=content == payload), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                manifest = {**official, 'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
                (root / 'voice-model.json').write_text(json.dumps(manifest))
                def download(url, path):
                    self.assertEqual(url, official['url'])
                    path.write_bytes(content)
                with patch.object(prepare_voice, 'ROOT', root), \
                        patch.object(prepare_voice.urllib.request, 'urlretrieve', side_effect=download) as fetch, \
                        patch('builtins.print'):
                    if content == payload:
                        prepare_voice.main()
                        self.assertEqual((root / 'app/src/main/assets/voice/model.zip').read_bytes(), payload)
                    else:
                        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                            prepare_voice.main()
                        self.assertFalse((root / 'app/src/main/assets/voice/model.zip').exists())
                    fetch.assert_called_once()

    def test_voice_does_not_fall_back_when_official_source_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = json.loads((prepare_voice.ROOT / 'voice-model.json').read_text())
            (root / 'voice-model.json').write_text(json.dumps(manifest))
            with patch.object(prepare_voice, 'ROOT', root), \
                    patch.object(prepare_voice.urllib.request, 'urlretrieve', side_effect=OSError('404')) as fetch:
                with self.assertRaises(OSError):
                    prepare_voice.main()
                fetch.assert_called_once()
                self.assertFalse((root / 'app/src/main/assets/voice/model.zip').exists())


if __name__ == '__main__':
    unittest.main()
