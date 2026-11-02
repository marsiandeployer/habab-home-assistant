import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import check_node_inputs
import package_runtime as runtime


class NodeInputTests(unittest.TestCase):
    def setUp(self):
        self.name = 'nodejs-24.22.1-r1.apk'
        self.payload = b'synthetic APK fixture'
        self.record = {'bytes': len(self.payload), 'sha256': hashlib.sha256(self.payload).hexdigest(),
                       'url': runtime.NODE_APK_REPOSITORY + '/' + self.name}

    def test_accepts_only_official_exact_package_url_and_rejects_mirrors(self):
        self.assertEqual(runtime.node_package_url(self.name, self.record, runtime.NODE_APK_REPOSITORY), self.record['url'])
        for url in ('https://habab-demo.wpmix.net/node.apk', self.record['url'] + '?x=1',
                    self.record['url'].replace('https:', 'http:')):
            with self.assertRaisesRegex(ValueError, 'official pinned'):
                runtime.node_package_url(self.name, {**self.record, 'url': url}, runtime.NODE_APK_REPOSITORY)
        with self.assertRaisesRegex(ValueError, 'official pinned'):
            runtime.node_package_url(self.name, {**self.record, 'mirrorUrl': self.record['url']}, runtime.NODE_APK_REPOSITORY)

    def test_official_download_retains_byte_and_sha_checks_before_apk_transaction(self):
        for downloaded in (self.payload, self.payload[:-1], b'x' * len(self.payload)):
            with self.subTest(corrupt=downloaded != self.payload), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / 'tools/network_tools').mkdir(parents=True)
                lock = {'schema': 1, 'architecture': 'aarch64', 'repository': runtime.NODE_APK_REPOSITORY,
                        'nodeVersion': '24.22.1-r1', 'alpineRelease': '3.24', 'packages': {self.name: self.record}}
                (root / 'tools/network_tools/node-apks.lock.json').write_text(json.dumps(lock))
                guest, cache = root / 'guest', root / 'cache'
                cache.mkdir()
                (guest / 'etc').mkdir(parents=True)
                (guest / 'etc/alpine-release').write_text('3.24.0\n')
                (guest / 'lib/apk/db').mkdir(parents=True)
                (guest / 'lib/apk/db/installed').write_text('P:nodejs\nV:24.22.1-r1\nA:aarch64\n\nP:icu-data-en\nV:78.1-r0\n')
                def download(url, path):
                    self.assertEqual(url, self.record['url'])
                    path.write_bytes(downloaded)
                with patch.object(runtime, 'ROOT', root), patch.object(runtime.urllib.request, 'urlretrieve', side_effect=download), \
                        patch.object(runtime.subprocess, 'run', return_value=runtime.subprocess.CompletedProcess([], 0, stdout='', stderr='')) as transaction:
                    if downloaded == self.payload:
                        result = runtime.install_node(guest, cache, runtime.NODE_APK_HELPER)
                        self.assertEqual(result, {self.name: self.record['sha256']})
                        transaction.assert_called()
                    else:
                        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                            runtime.install_node(guest, cache, runtime.NODE_APK_HELPER)
                        transaction.assert_not_called()
                        self.assertFalse((cache / 'node-apks' / self.name).exists())
                    self.assertFalse(list((cache / 'node-apks').glob('*.partial')))

    def test_preflight_uses_official_url_and_reports_package_when_unavailable(self):
        with patch.object(check_node_inputs.urllib.request, 'urlopen', side_effect=OSError('HTTP404')) as request:
            with self.assertRaisesRegex(RuntimeError, 'nodejs-24.22.1-r1.apk'):
                check_node_inputs.check_record((self.name, self.record))
            self.assertEqual(request.call_args.args[0].full_url, self.record['url'])
            self.assertEqual(request.call_args.args[0].method, 'HEAD')


    def test_native_and_vosk_preflight_uses_official_urls_and_locked_voice_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'native-packages.json').write_text(json.dumps({'proot': {'Filename': 'pool/main/p/proot/proot_5.1.107.102_aarch64.deb'}}))
            (root / 'voice-model.json').write_text(json.dumps({'name': 'model', 'url': 'https://alphacephei.com/vosk/models/model.zip', 'bytes': 42}))
            with patch.object(check_node_inputs, 'ROOT', root), patch.object(check_node_inputs, 'check_url') as check:
                check_node_inputs.check_native_and_voice_inputs()
                self.assertEqual(check.call_args_list[0].args, ('Termux proot', 'https://packages.termux.dev/apt/termux-main/pool/main/p/proot/proot_5.1.107.102_aarch64.deb'))
                self.assertEqual(check.call_args_list[1].args, ('Vosk model', 'https://alphacephei.com/vosk/models/model.zip', 42))
            voice = json.loads((root / 'voice-model.json').read_text())
            voice['url'] = 'https://habab-demo.wpmix.net/model.zip'
            (root / 'voice-model.json').write_text(json.dumps(voice))
            with patch.object(check_node_inputs, 'ROOT', root), patch.object(check_node_inputs, 'check_url'):
                with self.assertRaisesRegex(ValueError, 'official model URL'):
                    check_node_inputs.check_native_and_voice_inputs()

    def test_signed_index_rejects_changed_bytes_before_trusting_package_records(self):
        def tar_bytes(name, content):
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode='w:') as archive:
                member = tarfile.TarInfo(name)
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
            return output.getvalue()
        payload = gzip.compress(tar_bytes('APKINDEX', b'P:nodejs\nV:24.22.1-r1\nA:aarch64\n\n'), mtime=0)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = root / 'fixture-key.pem'
            subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:1024',
                            '-out', str(private)], check=True, capture_output=True)
            public = subprocess.check_output(['openssl', 'pkey', '-in', str(private), '-pubout'])
            signature = subprocess.check_output(['openssl', 'dgst', '-sha1', '-sign', str(private)], input=payload)
            raw = gzip.compress(tar_bytes('.SIGN.RSA.fixture.rsa.pub', signature), mtime=0) + payload
            entries = check_node_inputs.verify_signed_index(raw, public, 'fixture.rsa.pub')
            self.assertIn(self.name, entries)
            with self.assertRaises(subprocess.CalledProcessError):
                check_node_inputs.verify_signed_index(raw[:-1] + bytes([raw[-1] ^ 1]), public, 'fixture.rsa.pub')

    def test_signed_index_dependency_closure_rejects_old_ada_abi(self):
        ada = 'ada-libs-4.0.0-r0.apk'
        node_record = {**self.record, 'indexChecksum': 'Q1node'}
        ada_record = {**self.record, 'url': runtime.NODE_APK_REPOSITORY + '/' + ada, 'indexChecksum': 'Q1ada'}
        lock = {'architecture': 'aarch64', 'packages': {self.name: node_record, ada: ada_record}}
        entries = {
            self.name: {'P': 'nodejs', 'V': '24.22.1-r1', 'A': 'aarch64', 'S': str(self.record['bytes']),
                        'C': 'Q1node', 'D': 'so:libada.so.4'},
            ada: {'P': 'ada-libs', 'V': '4.0.0-r0', 'A': 'aarch64', 'S': str(self.record['bytes']),
                  'C': 'Q1ada', 'p': 'so:libada.so.4=4.0.0'},
        }
        check_node_inputs.validate_index_lock(lock, entries)
        entries[ada]['p'] = 'so:libada.so.3=3.3.0'
        with self.assertRaisesRegex(ValueError, 'Incomplete locked Alpine dependency'):
            check_node_inputs.validate_index_lock(lock, entries)
        entries[ada]['p'] = 'so:libada.so.4=4.0.0'
        entries[self.name]['C'] = 'Q1changed'
        with self.assertRaisesRegex(ValueError, 'differs from the signed official index'):
            check_node_inputs.validate_index_lock(lock, entries)


if __name__ == '__main__':
    unittest.main()
