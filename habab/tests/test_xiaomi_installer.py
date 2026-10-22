"""Pinned runtime installer keeps untrusted archives outside the HA config."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

source = Path(__file__).resolve().parents[1] / 'distribution/install-xiaomi-home.py'
spec = importlib.util.spec_from_file_location('xiaomi_installer', source)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def fixture(self, root, extra=None):
        commit = 'a'*40
        prefix = 'ha_xiaomi_home-'+commit+'/'
        stream = io.BytesIO()
        with zipfile.ZipFile(stream,'w') as z:
            z.writestr(prefix+'custom_components/xiaomi_home/manifest.json',json.dumps({'domain':'xiaomi_home','version':'fixture'}))
            for name in ('LICENSE.md','LegalNotice.md'): z.writestr(prefix+name,'fixture legal notice')
            if extra: z.writestr(prefix+'custom_components/xiaomi_home/'+extra,'fixture')
        data=stream.getvalue()
        release={'version':'fixture','commit':commit,'url':'https://codeload.github.com/XiaoMi/ha_xiaomi_home/zip/'+commit,'sha256':hashlib.sha256(data).hexdigest()}
        release_file=root/'release.json';release_file.write_text(json.dumps(release))
        return data,release_file

    def test_verified_install_preserves_legal_notices_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);data,release=self.fixture(root)
            with patch.object(installer.urllib.request,'urlopen',return_value=io.BytesIO(data)) as fetch:
                result=installer.install(root/'config',release)
                self.assertTrue(result['installed'])
                self.assertEqual((root/'config/custom_components/xiaomi_home/LICENSE.md').read_text(),'fixture legal notice')
                self.assertTrue(installer.install(root/'config',release)['already_installed'])
                fetch.assert_called_once()

    def test_integrity_failure_and_path_traversal_do_not_write_component(self):
        for extra in (None,'../../outside.txt'):
            with self.subTest(extra=extra),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);data,release=self.fixture(root,extra)
                if extra is None: data+=b'changed'
                with patch.object(installer.urllib.request,'urlopen',return_value=io.BytesIO(data)):
                    with self.assertRaises(ValueError): installer.install(root/'config',release)
                self.assertFalse((root/'config/custom_components/xiaomi_home').exists())
                self.assertFalse((root/'config/outside.txt').exists())

    def test_unknown_existing_installation_retained_without_download(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);_,release=self.fixture(root);existing=root/'config/custom_components/xiaomi_home'
            existing.mkdir(parents=True);(existing/'owner.txt').write_text('retained')
            with patch.object(installer.urllib.request,'urlopen') as fetch:
                with self.assertRaises(ValueError): installer.install(root/'config',release)
                fetch.assert_not_called()
            self.assertEqual((existing/'owner.txt').read_text(),'retained')
