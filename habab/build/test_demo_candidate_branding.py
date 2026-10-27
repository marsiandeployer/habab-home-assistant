"""Tests that candidate branding packaging includes every local index asset."""

import tempfile
import unittest
from pathlib import Path

from prepare_demo_candidate_branding import prepare_demo_candidate_branding
from publish_build import BRAND_ASSETS, BRAND_ASSET_DIR


class DemoCandidateBrandingTests(unittest.TestCase):
    def test_installs_assets_referenced_by_compiled_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            dist = Path(temporary)
            (dist / 'static').mkdir()
            (dist / 'index.html').write_text(
                '<link rel="icon" href="/static/habab/habab.svg">'
                '<meta content="/static/habab/habab-192.png">',
                encoding='utf-8',
            )

            references = prepare_demo_candidate_branding(dist)

            self.assertEqual(references, [
                '/static/habab/habab-192.png',
                '/static/habab/habab.svg',
            ])
            installed = dist / 'static' / 'habab'
            self.assertEqual({path.name for path in installed.iterdir()}, set(BRAND_ASSETS))
            for name in BRAND_ASSETS:
                self.assertEqual(
                    (installed / name).read_bytes(),
                    (BRAND_ASSET_DIR / name).read_bytes(),
                )

    def test_rejects_index_reference_outside_installed_brand_set(self):
        with tempfile.TemporaryDirectory() as temporary:
            dist = Path(temporary)
            (dist / 'static').mkdir()
            (dist / 'index.html').write_text(
                '<link rel="icon" href="/static/habab/missing.svg">',
                encoding='utf-8',
            )

            with self.assertRaisesRegex(ValueError, 'Missing compiled Habab brand asset'):
                prepare_demo_candidate_branding(dist)


if __name__ == '__main__':
    unittest.main()
