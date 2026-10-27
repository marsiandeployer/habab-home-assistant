"""Release payloads retain service metadata and bundled dependency notices."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from package_runtime import DIST, copy_sources


class RuntimeSourcePayloadTests(unittest.TestCase):
    def test_copies_beta_service_metadata_and_qr_license(self):
        with tempfile.TemporaryDirectory() as directory:
            rootfs = Path(directory)
            inventory = copy_sources(rootfs)
            for relative in (
                "beta.py", "services.yaml", "frontend/vendor/qrcode.LICENSE",
            ):
                source = DIST / "src/habab_home" / relative
                target = rootfs / "usr/src/homeassistant/homeassistant/components/habab_home" / relative
                with self.subTest(payload=relative):
                    self.assertTrue(target.is_file(), relative)
                    self.assertEqual(target.read_bytes(), source.read_bytes())
                    self.assertEqual(
                        inventory["src/habab_home/" + relative],
                        hashlib.sha256(source.read_bytes()).hexdigest(),
                    )


if __name__ == "__main__":
    unittest.main()
