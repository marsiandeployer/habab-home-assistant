"""Pinned Codex bundle extraction tests using tiny synthetic official assets."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch


DIST = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("codex_installer", DIST / "install-codex.py")
codex_installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(codex_installer)


class CodexInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.release = json.loads((DIST / "codex-release.json").read_text())
        self.assets = {}
        selected = self.release["arm64"]
        for name, record in selected["artifacts"].items():
            binary = ("fixture:" + name).encode()
            archive_io = io.BytesIO()
            with tarfile.open(fileobj=archive_io, mode="w:gz") as archive:
                member = tarfile.TarInfo(record["asset"][:-len(".tar.gz")])
                member.size = len(binary)
                archive.addfile(member, io.BytesIO(binary))
            content = archive_io.getvalue()
            record["bytes"] = len(content)
            record["sha256"] = hashlib.sha256(content).hexdigest()
            url = "https://github.com/openai/codex/releases/download/rust-v0.166.0/" + record["asset"]
            self.assets[url] = content
        self.release_file = self.root / "codex-release.json"
        self.release_file.write_text(json.dumps(self.release))
        self.output = self.root / "opt/habab-codex"

    def opener(self, url, timeout):
        self.assertEqual(timeout, 120)
        return io.BytesIO(self.assets[url])

    def test_installs_exact_arm64_cli_and_required_sibling_helpers(self):
        result = codex_installer.install(self.release_file, self.output, "arm64", opener=self.opener)
        self.assertEqual(result["version"], "0.166.0")
        for name in codex_installer.REQUIRED_FILES:
            data = (self.output / name).read_bytes()
            self.assertEqual(data, ("fixture:" + name).encode())
            self.assertEqual((self.output / name).stat().st_mode & 0o777, 0o755)
            self.assertEqual(result["files"][name], hashlib.sha256(data).hexdigest())
        self.assertEqual((self.output / "codex.sha256").read_text().strip(), result["sha256"])

    def test_matching_installed_bundle_is_reused_without_network(self):
        codex_installer.install(self.release_file, self.output, "arm64", opener=self.opener)
        with patch.object(codex_installer.urllib.request, "urlopen", side_effect=AssertionError("unexpected download")):
            codex_installer.install(self.release_file, self.output, "arm64")

    def test_missing_execute_bit_reinstalls_binary_bundle(self):
        codex_installer.install(self.release_file, self.output, "arm64", opener=self.opener)
        (self.output / "codex-exec-server").chmod(0o644)
        codex_installer.install(self.release_file, self.output, "arm64", opener=self.opener)
        self.assertEqual((self.output / "codex-exec-server").stat().st_mode & 0o777, 0o755)

    def test_malformed_digest_sidecar_repairs_the_installed_bundle(self):
        codex_installer.install(self.release_file, self.output, "arm64", opener=self.opener)
        (self.output / "codex.sha256").write_bytes(b"\xff")
        codex_installer.install(self.release_file, self.output, "arm64", opener=self.opener)
        self.assertEqual((self.output / "codex.sha256").read_text().strip(),
                         hashlib.sha256(b"fixture:codex").hexdigest())

    def test_corrupt_official_asset_is_rejected_before_replacing_existing_files(self):
        codex_installer.install(self.release_file, self.output, "arm64", opener=self.opener)
        before = {name: (self.output / name).read_bytes() for name in codex_installer.REQUIRED_FILES}
        bad_assets = dict(self.assets)
        first = next(iter(bad_assets))
        bad_assets[first] = b"corrupt"
        (self.output / "codex-code-mode-host").write_bytes(b"preserve existing during failed refresh")
        before["codex-code-mode-host"] = b"preserve existing during failed refresh"
        with self.assertRaisesRegex(ValueError, "size mismatch|SHA-256 mismatch"):
            codex_installer.install(self.release_file, self.output, "arm64",
                                    opener=lambda url, timeout: io.BytesIO(bad_assets[url]))
        self.assertEqual(before, {name: (self.output / name).read_bytes() for name in before})

    def test_unknown_architecture_and_changed_asset_name_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "Unsupported target"):
            codex_installer.load_release(self.release_file, "i386")
        self.release["arm64"]["artifacts"]["codex"]["asset"] = "other.tar.gz"
        self.release_file.write_text(json.dumps(self.release))
        with self.assertRaisesRegex(ValueError, "Invalid pinned official Codex artifact"):
            codex_installer.load_release(self.release_file, "arm64")


if __name__ == "__main__":
    unittest.main()
