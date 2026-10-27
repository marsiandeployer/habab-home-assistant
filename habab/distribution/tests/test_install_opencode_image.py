import base64
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

SCRIPT = Path(__file__).parents[1] / "install-opencode-image.py"
spec = importlib.util.spec_from_file_location("install_opencode_image", SCRIPT)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
install = installer.install


def archive_with_binary(contents: bytes) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.USTAR_FORMAT) as package:
            member = tarfile.TarInfo("package/bin/opencode")
            member.mode = 0o755
            member.size = len(contents)
            package.addfile(member, io.BytesIO(contents))
    return buffer.getvalue()


class FakeResponse:
    def __init__(self, content: bytes):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self.content if size < 0 else self.content[:size]


class InstallOpenCodeImageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.amd64_binary = b"synthetic-amd64-opencode"
        self.arm64_binary = b"synthetic-arm64-opencode"
        self.archives = {
            "amd64": archive_with_binary(self.amd64_binary),
            "arm64": archive_with_binary(self.arm64_binary),
        }
        self.urls = {
            "amd64": "https://registry.npmjs.org/opencode-linux-x64-baseline-musl/-/opencode-linux-x64-baseline-musl-1.2.3.tgz",
            "arm64": "https://registry.npmjs.org/opencode-linux-arm64-musl/-/opencode-linux-arm64-musl-1.2.3.tgz",
        }
        packages = {
            "amd64": "opencode-linux-x64-baseline-musl",
            "arm64": "opencode-linux-arm64-musl",
        }
        manifest = {}
        for arch, binary in (("amd64", self.amd64_binary), ("arm64", self.arm64_binary)):
            archive = self.archives[arch]
            manifest[arch] = {
                "package": packages[arch],
                "version": "1.2.3",
                "url": self.urls[arch],
                "integrity": "sha512-" + base64.b64encode(hashlib.sha512(archive).digest()).decode(),
            }
        self.manifest_path = self.root / "opencode-release.json"
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def test_installs_binary_and_writes_sha256(self):
        calls = []

        def opener(url, timeout):
            calls.append((url, timeout))
            return FakeResponse(self.archives["amd64"])

        output = self.root / "amd64"
        result = install(self.manifest_path, output, "amd64", opener=opener)
        installed = output / "opencode"
        digest = hashlib.sha256(self.amd64_binary).hexdigest()

        self.assertEqual(calls, [(self.urls["amd64"], 90)])
        self.assertEqual(installed.read_bytes(), self.amd64_binary)
        self.assertEqual(installed.stat().st_mode & 0o777, 0o755)
        self.assertEqual((output / "opencode.sha256").read_text(), digest + "\n")
        self.assertEqual(result, {"arch": "amd64", "version": "1.2.3", "sha256": digest})

    def test_rejects_tampered_sri_without_writing_binary(self):
        manifest = json.loads(self.manifest_path.read_text())
        manifest["amd64"]["integrity"] = "sha512-" + base64.b64encode(b"wrong").decode()
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        output = self.root / "rejected"

        with self.assertRaisesRegex(ValueError, "integrity check failed"):
            install(self.manifest_path, output, "amd64",
                    opener=lambda _url, timeout: FakeResponse(self.archives["amd64"]))

        self.assertFalse((output / "opencode").exists())

    def test_selects_arm64_manifest_entry(self):
        calls = []

        def opener(url, timeout):
            calls.append(url)
            return FakeResponse(self.archives["arm64"])

        output = self.root / "arm64"
        result = install(self.manifest_path, output, "arm64", opener=opener)

        self.assertEqual(calls, [self.urls["arm64"]])
        self.assertEqual((output / "opencode").read_bytes(), self.arm64_binary)
        self.assertEqual(result["arch"], "arm64")


if __name__ == "__main__":
    unittest.main()
