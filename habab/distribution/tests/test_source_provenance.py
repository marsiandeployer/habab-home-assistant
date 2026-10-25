"""Source labels must describe the checkout or explicit embedded release metadata."""

import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).parents[1] / "source_provenance.py"
spec = importlib.util.spec_from_file_location("source_provenance", SCRIPT)
source_provenance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(source_provenance)


class SourceProvenanceTests(unittest.TestCase):
    def test_checkout_uses_head_and_marks_whole_checkout_dirty(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            distribution = checkout / "habab" / "distribution"
            distribution.mkdir(parents=True)
            (distribution / "Dockerfile").write_text("FROM scratch\n")
            (distribution / "release.json").write_text("{}\n")
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            subprocess.run(["git", "-C", str(checkout), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(checkout), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
            subprocess.run(["git", "-C", str(checkout), "commit", "-qm", "fixture"], check=True)
            expected = subprocess.check_output(
                ["git", "-C", str(checkout), "rev-parse", "--verify", "HEAD"], text=True
            ).strip()

            self.assertEqual(
                source_provenance.resolve_source_provenance(distribution, {}),
                (expected, "false"),
            )
            (checkout / "outside-distribution.txt").write_text("whole checkout is dirty\n")
            self.assertEqual(
                source_provenance.resolve_source_provenance(distribution, {}),
                (expected, "true"),
            )

    def test_export_uses_embedded_revision_and_dirty_state(self):
        revision = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                source_provenance.resolve_source_provenance(
                    Path(directory), {"source_revision": revision, "source_dirty": False}
                ),
                (revision, "false"),
            )

    def test_export_without_valid_provenance_stays_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                source_provenance.resolve_source_provenance(
                    Path(directory), {"source_revision": "upstream/dev", "source_dirty": "no"}
                ),
                ("unknown", "unknown"),
            )

    def test_explicit_export_environment_is_validated(self):
        revision = "b" * 40
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                source_provenance.resolve_source_provenance(
                    Path(directory), {},
                    {"HABAB_SOURCE_REVISION": revision, "HABAB_SOURCE_DIRTY": "true"},
                ),
                (revision, "true"),
            )
            self.assertEqual(
                source_provenance.resolve_source_provenance(
                    Path(directory), {},
                    {"HABAB_SOURCE_REVISION": "HEAD", "HABAB_SOURCE_DIRTY": "yes"},
                ),
                ("unknown", "unknown"),
            )

    def test_release_overlay_does_not_mutate_package_metadata(self):
        release = {"product": "Habab Home", "version": "0.3.4"}
        overlay = source_provenance.release_with_source(release, "c" * 40, "true")
        self.assertEqual(release, {"product": "Habab Home", "version": "0.3.4"})
        self.assertEqual(overlay["source_revision"], "c" * 40)
        self.assertEqual(overlay["source_dirty"], "true")


if __name__ == "__main__":
    unittest.main()
