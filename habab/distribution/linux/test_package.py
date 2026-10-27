import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

import package


class NativePackageTests(unittest.TestCase):
    def test_package_is_repeatable_and_binds_frontend_to_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            version = "20260930.4+habab.19"
            wheel = root / f"home_assistant_frontend-{version}-py3-none-any.whl"
            wheel.write_bytes(b"frontend package fixture")
            manifest = root / "manifest.json"
            metadata = {
                "source_revision": "a" * 40, "source_clean_before_build": True,
                "builder_checks": "passed", "wheel_version": version,
                "artifact": wheel.name, "artifact_bytes": wheel.stat().st_size,
                "artifact_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            }
            manifest.write_text(json.dumps(metadata))
            first = package.build("a" * 40, wheel, manifest, root / "first.tar.gz")
            second = package.build("a" * 40, wheel, manifest, root / "second.tar.gz")
            self.assertEqual(first["archive_sha256"], second["archive_sha256"])
            with tarfile.open(root / "first.tar.gz") as archive:
                names = archive.getnames()
                self.assertIn("payload/app/habab-ha.py", names)
                self.assertIn("payload/app/xiaomi-home-release.json", names)
                self.assertFalse(any("__pycache__" in name or name.endswith(".pyc") for name in names))
                release = json.load(archive.extractfile("payload/release.json"))
                self.assertEqual(release["source_sha"], "a" * 40)
                self.assertEqual(release["frontend_sha256"], metadata["artifact_sha256"])
            metadata["source_revision"] = "b" * 40
            manifest.write_text(json.dumps(metadata))
            with self.assertRaisesRegex(ValueError, "exact clean source"):
                package.build("a" * 40, wheel, manifest, root / "wrong.tar.gz")


if __name__ == "__main__":
    unittest.main()
