"""Focused tests for Habab demo branding publisher transforms."""

import tempfile
import unittest
from pathlib import Path

from publish_build import BRAND_ASSETS, BRAND_ASSET_DIR, brand_demo_manifest, install_brand_assets, scope_html

REPO = Path(__file__).resolve().parents[2]


class DemoBrandingPublisherTests(unittest.TestCase):
    def test_template_keeps_habab_identity_and_home_assistant_loaders(self):
        template = (REPO / 'demo/src/html/index.html.template').read_text(encoding='utf-8')
        self.assertIn('<title>Habab Home Assistant</title>', template)
        self.assertIn('/static/habab/habab.svg', template)
        self.assertIn('Сделано в habab.ru', template)
        self.assertIn('_script_loader.html.template', template)
        self.assertIn('dev_inspect', template)
        self.assertIn('sha256-HICOTHvVU35lGtBA+jMBomP8mgvMi3ZdwpmHYF8YKbw=', template)
        self.assertNotIn('/static/icons/', template)

    def test_social_metadata_uses_habab_demo_and_image(self):
        social = (REPO / 'demo/src/html/_social_meta.html.template').read_text(encoding='utf-8')
        self.assertIn('content="Habab Home Assistant"', social)
        self.assertIn('https://habab-demo.wpmix.net/', social)
        self.assertIn('https://habab.ru/static/brand/habab-social.webp', social)

    def test_manifest_has_full_brand_and_only_staged_icons(self):
        manifest = brand_demo_manifest({'name': 'Home Assistant', 'short_name': 'HA', 'icons': [{'src': '/static/icons/favicon.ico'}]})
        self.assertEqual(manifest['name'], 'Habab Home Assistant')
        self.assertEqual(manifest['short_name'], 'Habab Home')
        self.assertEqual(manifest['description'], 'Демо-интерфейс Habab Home Assistant для умного дома.')
        self.assertEqual(manifest['theme_color'], '#155e4b')
        self.assertEqual([icon['src'] for icon in manifest['icons']], [
            './static/habab/habab-192.png', './static/habab/habab-512.png',
            './static/habab/habab-maskable-512.png',
        ])

    def test_publisher_installs_each_icon_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            (stage / 'static').mkdir()
            install_brand_assets(stage)
            destination = stage / 'static' / 'habab'
            self.assertEqual({path.name for path in destination.iterdir()}, set(BRAND_ASSETS))
            for name in BRAND_ASSETS:
                self.assertEqual((destination / name).read_bytes(), (BRAND_ASSET_DIR / name).read_bytes())

    def test_publisher_rejects_conflicting_compiled_icon_set(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            destination = stage / 'static' / 'habab'
            destination.mkdir(parents=True)
            for name in BRAND_ASSETS:
                (destination / name).write_bytes((BRAND_ASSET_DIR / name).read_bytes())
            (destination / BRAND_ASSETS[0]).write_bytes(b'conflict')
            with self.assertRaisesRegex(ValueError, 'Conflicting staged Habab brand asset'):
                install_brand_assets(stage)

    def test_title_and_icons_are_scoped_to_immutable_release(self):
        source = ('<html><head><title>Home Assistant Demo</title></head>'
                  '<body><img src="/static/habab/habab.svg"><link rel="manifest" href="/manifest.json"></body></html>')
        published = scope_html(source)
        self.assertIn('<title>Habab Home Assistant</title>', published)
        self.assertIn('/demos/home-assistant/releases/', published)
        self.assertIn('/static/habab/habab.svg', published)
        self.assertNotIn('<title>Home Assistant Demo</title>', published)


if __name__ == '__main__':
    unittest.main()
