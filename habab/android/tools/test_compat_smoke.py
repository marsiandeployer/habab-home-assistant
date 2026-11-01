"""Offline runner contracts; these tests never start ADB, SDK tools or an emulator."""
import hashlib
import io
import json
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

import compat_smoke as smoke


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.destination = Path(self.directory.name) / smoke.ASSET
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("lib/arm64-v8a/libtest.so", b"fixture only")
        self.payload = buffer.getvalue()
        self.digest = hashlib.sha256(self.payload).hexdigest()
        self.release = {"tag_name": "apk59", "draft": False, "published_at": "2026-11-01T00:00:00Z",
                        "assets": [{"name": smoke.ASSET, "state": "uploaded", "id": 59,
                                    "size": len(self.payload), "digest": "sha256:" + self.digest,
                                    "browser_download_url": "https://github.com/" + smoke.REPOSITORY
                                    + "/releases/download/apk59/" + smoke.ASSET}]}

    def fetch(self, payload=None, digest=None):
        opener = Mock()
        opener.open.return_value = io.BytesIO(self.payload if payload is None else payload)
        with patch.object(smoke.urllib.request, "urlopen", return_value=io.BytesIO(
                json.dumps(self.release).encode())) as metadata, patch.object(
                smoke.urllib.request, "build_opener", return_value=opener):
            result = smoke.download("apk59", digest or self.digest,
                                    self.release["assets"][0]["size"], self.destination)
        return result, metadata, opener

    def test_pinned_public_arm64_asset(self):
        result, metadata, opener = self.fetch()
        self.assertEqual(result["abis"], ["arm64-v8a"])
        self.assertEqual(result["asset_id"], 59)
        self.assertEqual(self.destination.read_bytes(), self.payload)
        self.assertEqual(metadata.call_args.args[0].get_header("Authorization"), None)
        self.assertEqual(opener.open.call_args.args[0], self.release["assets"][0]["browser_download_url"])

    def test_existing_file_is_preserved_before_network(self):
        self.destination.write_bytes(b"caller owned")
        with patch.object(smoke.urllib.request, "urlopen") as network:
            with self.assertRaises(smoke.Failure) as caught:
                smoke.download("apk59", self.digest, len(self.payload), self.destination)
        self.assertEqual(caught.exception.code, "output_apk_already_exists")
        self.assertEqual(self.destination.read_bytes(), b"caller owned")
        network.assert_not_called()

    def test_main_failure_cleanup_preserves_caller_apk(self):
        self.destination.write_bytes(b"caller owned")
        with patch("sys.argv", ["compat_smoke.py", "--api", "35", "--out", self.directory.name]), \
                patch.dict(smoke.os.environ, {"HABAB_COMPAT_APK_BYTES": str(len(self.payload)),
                                             "HABAB_COMPAT_RELEASE_TAG": "apk59",
                                             "HABAB_COMPAT_APK_SHA256": self.digest}), \
                patch("sys.stdout", new_callable=io.StringIO), patch.object(smoke.os, "umask"), \
                patch.object(smoke.urllib.request, "urlopen") as network:
            self.assertEqual(smoke.main(), 1)
        self.assertEqual(self.destination.read_bytes(), b"caller owned")
        report = json.loads((self.destination.parent / "report.json").read_text())
        self.assertEqual(report["failure_code"], "output_apk_already_exists")
        self.assertTrue(report["emulator_stopped"])
        network.assert_not_called()

    def test_release_metadata_must_match_pin(self):
        for field, value, code in (("digest", "sha256:" + "0" * 64, "release_apk_digest_mismatch"),
                                   ("id", None, "invalid_release_asset_id"),
                                   ("browser_download_url", "https://example.org/apk", "unexpected_release_apk_url")):
            with self.subTest(field=field):
                original = self.release["assets"][0][field]
                self.release["assets"][0][field] = value
                with self.assertRaises(smoke.Failure) as caught:
                    self.fetch()
                self.assertEqual(caught.exception.code, code)
                self.assertFalse(self.destination.exists())
                self.release["assets"][0][field] = original

    def test_bad_download_removes_only_owned_partial(self):
        damaged = bytes([self.payload[0] ^ 1]) + self.payload[1:]
        with self.assertRaises(smoke.Failure) as caught:
            self.fetch(payload=damaged)
        self.assertEqual(caught.exception.code, "download_bytes_or_digest_mismatch")
        self.assertFalse(self.destination.exists())

    def test_pinned_non_apk_is_rejected_and_removed(self):
        self.payload = b"not an APK"
        self.digest = hashlib.sha256(self.payload).hexdigest()
        self.release["assets"][0].update(size=len(self.payload), digest="sha256:" + self.digest)
        with self.assertRaises(smoke.Failure) as caught:
            self.fetch()
        self.assertEqual(caught.exception.code, "invalid_apk_archive")
        self.assertFalse(self.destination.exists())


class RunnerTests(unittest.TestCase):
    def test_public_http_retry_is_once_and_only_transient(self):
        for status in (429, 500, 503, 404, 403):
            error = smoke.urllib.error.HTTPError("https://example.org", status, "fixture", {}, None)
            opener = Mock(side_effect=[error, "success"])
            with self.subTest(status=status), patch.object(smoke.time, "sleep") as pause, \
                    patch("sys.stdout", new_callable=io.StringIO):
                if status == 429 or status >= 500:
                    self.assertEqual(smoke.public_open(opener, "request", 30), "success")
                    self.assertEqual(opener.call_count, 2)
                    pause.assert_called_once_with(2)
                else:
                    with self.assertRaises(smoke.urllib.error.HTTPError):
                        smoke.public_open(opener, "request", 30)
                    self.assertEqual(opener.call_count, 1)
                    pause.assert_not_called()
        error = smoke.urllib.error.HTTPError("https://example.org", 503, "fixture", {}, None)
        opener = Mock(side_effect=[error, error])
        with patch.object(smoke.time, "sleep"), patch("sys.stdout", new_callable=io.StringIO), \
                self.assertRaises(smoke.urllib.error.HTTPError):
            smoke.public_open(opener, "request", 30)
        self.assertEqual(opener.call_count, 2)

    def test_failed_tool_retains_bounded_redacted_diagnostics(self):
        failed = subprocess.CompletedProcess(["sdkmanager"], 1, "x" * 5000,
                                             "password=private https://example.org/signed?secret=value")
        with patch.object(smoke.subprocess, "run", return_value=failed):
            with self.assertRaises(smoke.Failure) as caught:
                smoke.run(["/sdk/cmdline-tools/latest/bin/sdkmanager", "--install", "emulator"])
        info = caught.exception.diagnostics
        self.assertEqual(info["tool"], "sdkmanager")
        self.assertEqual(info["exit_code"], 1)
        self.assertLessEqual(len(info["output_tail"]), 4000)
        self.assertNotIn("private", info["output_tail"])
        self.assertNotIn("secret=value", info["output_tail"])

    def test_api_argument_rejected_before_work(self):
        with patch("sys.argv", ["compat_smoke.py", "--api", "34", "--out", "/unused"]), \
                patch("sys.stderr", new_callable=io.StringIO), patch.object(smoke, "download") as fetch:
            with self.assertRaises(SystemExit) as caught:
                smoke.main()
        self.assertEqual(caught.exception.code, 2)
        fetch.assert_not_called()

    def test_image_metadata_records_fields_without_rejecting_optional_format(self):
        values = {"AndroidVersion.ApiLevel": "35", "SystemImage.Abi": "x86_64",
                  "SystemImage.TagId": "google_apis", "SystemImage.VendorId": "google", "Pkg.Revision": "1"}
        for api in ("35", "36"):
            values["AndroidVersion.ApiLevel"] = api
            for separator in ("=", " = "):
                text = "\n".join(key + separator + value for key, value in values.items())
                self.assertEqual(smoke.system_image_metadata(text), values)
        self.assertEqual(smoke.system_image_metadata("AndroidVersion.ApiLevel=36")['SystemImage.VendorId'], "")
        self.assertEqual(smoke.system_image_metadata("")['AndroidVersion.ApiLevel'], "")

    def test_cleanup_still_stops_group_after_launcher_exit(self):
        process = Mock(pid=1234)
        process.poll.return_value = 0
        with patch.object(smoke, "run") as command, patch.object(smoke.os, "killpg") as kill:
            result = smoke.cleanup_emulator(process, "/sdk/adb")
        command.assert_called_once_with(["/sdk/adb", "-s", smoke.SERIAL, "emu", "kill"], timeout=15, check=False)
        self.assertTrue(result["emulator_stopped"])
        kill.assert_any_call(1234, signal.SIGKILL)

    def test_cleanup_uses_owned_group_when_adb_unavailable(self):
        process = Mock(pid=1234)
        process.wait.side_effect = [subprocess.TimeoutExpired("emulator", 15), 0]
        process.poll.return_value = 0
        with patch.object(smoke, "run", side_effect=smoke.Failure("INFRA_UNAVAILABLE", "tool_unavailable_or_timed_out")), \
                patch.object(smoke.os, "killpg") as kill:
            result = smoke.cleanup_emulator(process, "/sdk/adb")
        self.assertTrue(result["emulator_stopped"])
        self.assertTrue(result["adb_cleanup_unavailable"])
        kill.assert_any_call(1234, signal.SIGTERM)
        kill.assert_any_call(1234, signal.SIGKILL)

    def test_cleanup_failure_is_reported(self):
        process = Mock(pid=1234)
        with patch.object(smoke.os, "killpg", side_effect=PermissionError):
            self.assertFalse(smoke.cleanup_emulator(process, None)["emulator_stopped"])
        self.assertTrue(smoke.cleanup_emulator(None, None)["emulator_stopped"])


if __name__ == "__main__":
    unittest.main()
