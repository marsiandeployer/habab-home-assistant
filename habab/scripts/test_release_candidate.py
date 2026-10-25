import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile


SCRIPT = Path(__file__).with_name("release-candidate.py")
SPEC = importlib.util.spec_from_file_location("release_candidate", SCRIPT)
release_candidate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_candidate)


class ReleaseCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        temporary = Path(self.temporary.name)
        self.root = temporary / "artifacts"
        self.root.mkdir()
        self.source_tree = temporary / "source"
        distribution = self.source_tree / "habab/distribution"
        distribution.mkdir(parents=True)
        (distribution / "release.json").write_text(
            json.dumps({"product": "Habab Home", "version": "0.1"}, indent=2) + "\n",
            encoding="utf-8",
        )
        (distribution / "start.bat").write_bytes(b"@echo off\nstart\n")
        self.revision = "a" * 40

    def tearDown(self):
        self.temporary.cleanup()

    def add_status(self, target, state="built", **extra):
        directory = self.root / target
        directory.mkdir(parents=True, exist_ok=True)
        status = {"state": state, "source_revision": self.revision, "source_dirty": False, **extra}
        (directory / "status.json").write_text(json.dumps(status), encoding="utf-8")
        if state == "built":
            (directory / "artifact.bin").write_bytes(b"artifact:" + target.encode())

    def fill_targets(self, android_state="blocked"):
        self.add_windows_package()
        self.add_status("linux-amd64")
        self.add_status("linux-arm64")
        self.add_status("android", android_state,
                        **({"reason": "signed runtime input is not available"} if android_state == "blocked" else {}))

    def add_windows_package(self):
        directory = self.root / "windows"
        directory.mkdir(parents=True, exist_ok=True)
        source = self.source_tree / "habab/distribution"
        release = json.loads((source / "release.json").read_text(encoding="utf-8"))
        release.update({"source_revision": self.revision, "source_dirty": "false"})
        release_bytes = (json.dumps(release, indent=2) + "\n").encode()
        filename = "habab-home-windows-test.zip"
        with zipfile.ZipFile(directory / filename, "w", compression=zipfile.ZIP_DEFLATED) as package:
            package.writestr("release.json", release_bytes)
            package.writestr("start.bat", b"@echo off\r\nstart\r\n")
        data = (directory / filename).read_bytes()
        metadata = {"filename": filename, "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data), "files": ["release.json", "start.bat"]}
        (directory / "windows-package.json").write_text(json.dumps(metadata), encoding="utf-8")
        for name in ("start-home-assistant.bat", "start-xiaomi-home.bat", "support_tunnel.bat"):
            (directory / name).write_text(f"{filename} {metadata['sha256']}", encoding="ascii")
        status = {"state": "built", "source_revision": self.revision,
                  "source_dirty": False, "kind": "installer-package", "filename": filename}
        (directory / "status.json").write_text(json.dumps(status), encoding="utf-8")

    def test_assembles_hashes_and_keeps_android_blocked_explicit(self):
        self.fill_targets()

        manifest = release_candidate.assemble(self.root, self.revision, "blocked", self.source_tree)

        self.assertFalse(manifest["source_dirty"])
        self.assertEqual(manifest["targets"]["android"]["state"], "blocked")
        self.assertEqual(len(manifest["artifacts"]), 7)
        hashes = {item["path"]: item["sha256"] for item in manifest["artifacts"]}
        self.assertEqual(hashes["linux-amd64/artifact.bin"],
                         release_candidate._digest(self.root / "linux-amd64/artifact.bin")[0])
        records = {item["path"]: item for item in manifest["artifacts"]}
        self.assertEqual(records["linux-amd64/artifact.bin"]["artifact"], "candidate-linux-amd64")
        self.assertEqual(manifest["targets"]["windows"]["artifact"], "candidate-windows")

    def test_rejects_target_with_a_different_source_revision(self):
        self.fill_targets()
        path = self.root / "linux-arm64/status.json"
        status = json.loads(path.read_text(encoding="utf-8"))
        status["source_revision"] = "b" * 40
        path.write_text(json.dumps(status), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "another source revision"):
            release_candidate.assemble(self.root, self.revision, "blocked", self.source_tree)

    def test_rejects_dirty_source_attestation(self):
        self.fill_targets()
        path = self.root / "windows/status.json"
        status = json.loads(path.read_text(encoding="utf-8"))
        status["source_dirty"] = True
        path.write_text(json.dumps(status), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "clean source tree"):
            release_candidate.assemble(self.root, self.revision, "blocked", self.source_tree)

    def test_rejects_windows_package_that_breaks_source_copy_contract(self):
        self.fill_targets()
        path = self.root / "windows/habab-home-windows-test.zip"
        release = json.loads((self.source_tree / "habab/distribution/release.json").read_text(encoding="utf-8"))
        release.update({"source_revision": self.revision, "source_dirty": "false"})
        release_bytes = (json.dumps(release, indent=2) + "\n").encode()
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
            package.writestr("release.json", release_bytes)
            package.writestr("start.bat", b"@echo off\r\nchanged\r\n")
        metadata_path = self.root / "windows/windows-package.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        data = path.read_bytes()
        metadata.update({"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "differs from selected source"):
            release_candidate.assemble(self.root, self.revision, "blocked", self.source_tree)


if __name__ == "__main__":
    unittest.main()
