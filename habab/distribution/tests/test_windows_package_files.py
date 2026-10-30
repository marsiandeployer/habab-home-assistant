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
    def test_windows_kit_stages_only_the_canonical_tunnel_agent(self):
        names = set(collect_windows_package_names(DISTRIBUTION))
        canonical = 'product-tunnel/web_tunnel.py'
        self.assertIn(canonical, names)
        self.assertTrue(resolve_windows_package_source(DISTRIBUTION, canonical).is_file())
        self.assertIn('src/habab_product_tunnel/config_flow.py', names)
        self.assertIn('src/habab_product_tunnel/client.py', names)
        self.assertIn('src/habab_product_tunnel/translations/ru.json', names)
        self.assertFalse({
            'product-tunnel/home.json',
            'product-tunnel/identity',
            'product-tunnel/manage_home.py',
            'product-tunnel/sshd-home.example.conf',
            'product-tunnel/nginx-home.example.conf',
            'product-tunnel/connect.ps1',
        } & names)
        self.assertTrue({'support_tunnel.bat', 'support_tunnel.ps1', 'support-relay.json'} <= names)
        self.assertEqual(
            resolve_windows_package_source(DISTRIBUTION, canonical),
            DISTRIBUTION.parent / canonical,
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

    def test_missing_canonical_product_tunnel_source_fails_instead_of_omitting_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            distribution = Path(temporary) / 'distribution'
            distribution.mkdir()

            with self.assertRaisesRegex(
                ValueError,
                'Windows package source is missing or not a regular file: product-tunnel/web_tunnel.py',
            ):
                resolve_windows_package_source(distribution, 'product-tunnel/web_tunnel.py')

    def test_docker_build_uses_the_canonical_agent_staged_by_both_windows_paths(self):
        dockerfile = (DISTRIBUTION / 'Dockerfile').read_text()
        startup = (DISTRIBUTION / 'start-windows.ps1').read_text()
        self.assertIn(
            'COPY product-tunnel/web_tunnel.py /usr/src/homeassistant/homeassistant/components/habab_product_tunnel/web_tunnel.py',
            dockerfile,
        )
        self.assertIn("'product-tunnel\\web_tunnel.py'", startup)
        self.assertIn('Resolve-Path -LiteralPath $stagedTunnel', startup)
        compose = (DISTRIBUTION / 'compose.windows.yaml').read_text()
        self.assertIn('context: .', compose)


if __name__ == '__main__':
    unittest.main()
