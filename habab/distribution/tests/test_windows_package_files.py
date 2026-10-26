import re
import sys
import tempfile
from pathlib import Path
import unittest


DISTRIBUTION = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DISTRIBUTION))

from windows_package_files import (  # noqa: E402
    collect_windows_package_names,
    resolve_windows_package_source,
)


class WindowsPackageFilesTests(unittest.TestCase):
    def test_windows_kit_includes_manual_product_tunnel_client_inputs_only(self):
        names = set(collect_windows_package_names(DISTRIBUTION))
        required = {
            'product-tunnel/control.ps1',
            'product-tunnel/connect.ps1',
            'product-tunnel/home.example.json',
        }

        self.assertTrue(required <= names)
        self.assertTrue(all((DISTRIBUTION.parent / name).is_file() for name in required))
        self.assertFalse({
            'product-tunnel/home.json',
            'product-tunnel/identity',
            'product-tunnel/manage_home.py',
            'product-tunnel/sshd-home.example.conf',
            'product-tunnel/nginx-home.example.conf',
        } & names)
        for name in required:
            self.assertEqual(
                resolve_windows_package_source(DISTRIBUTION, name),
                DISTRIBUTION.parent / name,
            )

        with self.assertRaisesRegex(ValueError, 'Unsafe Windows package member path'):
            resolve_windows_package_source(DISTRIBUTION, '../product-tunnel/identity')

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'habab'
            distribution = root / 'distribution'
            (distribution / 'product-tunnel').mkdir(parents=True)
            (distribution / 'product-tunnel/identity').write_text('distribution source')
            (root / 'product-tunnel').mkdir()
            (root / 'product-tunnel/identity').write_text('external private key')
            self.assertEqual(
                resolve_windows_package_source(distribution, 'product-tunnel/identity'),
                distribution / 'product-tunnel/identity',
            )

    def test_registered_support_panel_and_referenced_video_captions_are_packaged(self):
        # A successfully built ZIP can still omit a sidebar module or captions.
        # Resolve the URLs used by the product against the actual collector.
        names = set(collect_windows_package_names(DISTRIBUTION))
        registration = (DISTRIBUTION / 'src/habab_home/__init__.py').read_text()
        support = re.search(r'module_url="(/habab-home/support-panel[^"?]+)(?:\?[^" ]*)?"', registration)
        self.assertIsNotNone(support)
        support_member = 'src/habab_home/frontend/' + support[1].removeprefix('/habab-home/')
        self.assertIn(support_member, names)
        self.assertTrue(resolve_windows_package_source(DISTRIBUTION, support_member).is_file())

        telegram_member = 'src/habab_home/frontend/telegram-panel.js'
        self.assertIn(telegram_member, names)
        telegram = resolve_windows_package_source(DISTRIBUTION, telegram_member).read_text()
        captions = re.findall(r'["\']\./([^"\']+\.vtt)["\']', telegram)
        self.assertTrue(captions, 'The video must retain its local captions')
        for caption in captions:
            member = 'src/habab_home/frontend/' + caption
            self.assertIn(member, names)
            self.assertTrue(resolve_windows_package_source(DISTRIBUTION, member).read_bytes().startswith(b'WEBVTT'))

    def test_missing_product_tunnel_source_fails_instead_of_omitting_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            distribution = Path(temporary) / 'distribution'
            distribution.mkdir()

            with self.assertRaisesRegex(
                ValueError,
                'Windows package source is missing or not a regular file: product-tunnel/connect.ps1',
            ):
                resolve_windows_package_source(distribution, 'product-tunnel/connect.ps1')


if __name__ == '__main__':
    unittest.main()
