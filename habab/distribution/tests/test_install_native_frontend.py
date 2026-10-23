"""A failed integrity check must never reach the package installer."""

import hashlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).parents[1] / "install-native-frontend.py"
spec = importlib.util.spec_from_file_location("install_native_frontend", SCRIPT)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class Response(io.BytesIO):
    def geturl(self):
        return installer.ARTIFACT_ORIGIN + "/downloads/native-frontend/wheel"


def release_for(data):
    digest = hashlib.sha256(data).hexdigest()
    filename = "home_assistant_frontend-20260930.4+habab.1-py3-none-any.whl"
    return {
        "version": "20260930.4+habab.1", "filename": filename,
        "bytes": len(data), "sha256": digest,
        "url": f"{installer.ARTIFACT_ORIGIN}/downloads/native-frontend/{digest}/{filename}",
    }


class NativeFrontendInstallerTests(unittest.TestCase):
    def test_verified_download(self):
        data = b"test wheel bytes"
        with tempfile.TemporaryDirectory() as directory:
            path = installer.download_wheel(release_for(data), directory,
                                            lambda *_args, **_kwargs: Response(data))
            self.assertEqual(path.read_bytes(), data)

    def test_rejects_corruption_truncation_and_extra_bytes(self):
        release = release_for(b"original wheel")
        for body in (b"replaced wheel", b"original", b"original wheel more"):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    installer.download_wheel(release, directory,
                                              lambda *_args, **_kwargs: Response(body))

    def test_rejects_untrusted_url_filename_and_unbounded_download(self):
        original = release_for(b"wheel")
        for patch in (
            {"url": "https://example.org/wheel"},
            {"filename": "../escape.whl"},
            {"version": "20260930.4"},
            {"sha256": "x" * 64},
            {"bytes": installer.MAX_WHEEL_BYTES + 1},
            {"bytes": True},
        ):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                installer.validate_release({**original, **patch})

    def test_rejects_cross_origin_redirect(self):
        class Redirect(Response):
            def geturl(self):
                return "https://example.org/wheel"

        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
            installer.download_wheel(release_for(b"wheel"), directory,
                                      lambda *_args, **_kwargs: Redirect(b"wheel"))


if __name__ == "__main__":
    unittest.main()
