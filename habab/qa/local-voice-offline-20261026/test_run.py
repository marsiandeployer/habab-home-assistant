from contextlib import redirect_stderr
from io import StringIO
import socket
import tempfile
import unittest
import wave
from pathlib import Path

from run import (
    GIB,
    MIN_FREE_BYTES,
    GuardError,
    check_disk_budget,
    check_loopback_port_available,
    count_successful_chat_requests,
    digest_matches,
    is_local_endpoint,
    is_local_filesystem,
    model_blob_digest,
    parse_mode,
    _validate_synthetic_wav,
    _ensure_fixture_scope,
)


class RunnerGuardTests(unittest.TestCase):
    def test_operator_must_choose_exactly_one_mode(self):
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                parse_mode([])
            with self.assertRaises(SystemExit):
                parse_mode(["--prepare", "--run-offline"])
        self.assertEqual(parse_mode(["--prepare"]), "prepare")
        self.assertEqual(parse_mode(["--run-offline"]), "run-offline")
        self.assertEqual(parse_mode(["--run-synthetic-turn"]), "run-synthetic-turn")

    def test_disk_floor_rejects_current_known_free_space(self):
        with self.assertRaises(GuardError):
            check_disk_budget(22 * GIB + GIB // 2)
        check_disk_budget(MIN_FREE_BYTES)

    def test_remote_docker_endpoints_and_network_storage_are_rejected(self):
        self.assertTrue(is_local_endpoint("unix:///var/run/docker.sock"))
        self.assertFalse(is_local_endpoint("ssh://server.example"))
        self.assertFalse(is_local_endpoint("tcp://10.0.0.2:2376"))
        self.assertTrue(is_local_filesystem("ext4"))
        self.assertTrue(is_local_filesystem("xfs"))
        self.assertTrue(is_local_filesystem("f2fs"))
        self.assertTrue(is_local_filesystem("overlay"))
        self.assertFalse(is_local_filesystem("nfs4"))
        self.assertFalse(is_local_filesystem("cifs"))
        self.assertFalse(is_local_filesystem("fuse.sshfs"))
        self.assertFalse(is_local_filesystem("fuse.rclone"))
        self.assertFalse(is_local_filesystem("unknownfs"))

    def test_loopback_bind_guard_rejects_an_occupied_port(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", 0))
            with self.assertRaises(GuardError):
                check_loopback_port_available(listener.getsockname()[1])
        finally:
            listener.close()

    def test_compose_is_confined_to_pinned_loopback_fixture(self):
        _ensure_fixture_scope()

    def test_ollama_request_counter_accepts_only_successful_chat_route_records(self):
        logs = "\n".join(
            (
                '2026-10-26T12:00:00.100000Z [GIN] 2026/10/26 - 12:00:00 | 200 | 2.1s | 172.20.0.2 | POST "/api/chat"',
                '2026-10-26T12:00:01.100000Z [GIN] 2026/10/26 - 12:00:01 | 500 | 1.0ms | 172.20.0.2 | POST "/api/chat"',
                '2026-10-26T12:00:02.100000Z [GIN] 2026/10/26 - 12:00:02 | 200 | 1.0ms | 172.20.0.2 | POST "/api/generate"',
                'unrelated line mentions POST "/api/chat" but is not a Gin access record',
            )
        )
        self.assertEqual(count_successful_chat_requests(logs), 1)

    def test_synthetic_wav_guard_requires_mono_pcm16(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            valid_path = f"{temp_dir}/mono.wav"
            with wave.open(valid_path, "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16_000)
                output.writeframes(b"\0\0" * 160)
            self.assertEqual(_validate_synthetic_wav(Path(valid_path)), 16_000)

            stereo_path = f"{temp_dir}/stereo.wav"
            with wave.open(stereo_path, "wb") as output:
                output.setnchannels(2)
                output.setsampwidth(2)
                output.setframerate(16_000)
                output.writeframes(b"\0\0\0\0" * 160)
            with self.assertRaises(GuardError):
                _validate_synthetic_wav(Path(stereo_path))

    def test_cached_model_files_and_ollama_manifest_use_expected_digests(self):
        expected = "sha256:85e4a5b7b8ef0e48af0e8658f5aaab9c2324c76c1641493f4d1e25fce54b18b9"
        manifest = {"layers": [{"mediaType": "application/vnd.ollama.image.model", "digest": expected}]}
        self.assertEqual(model_blob_digest(manifest), expected)
        self.assertIsNone(model_blob_digest({"layers": []}))
        self.assertTrue(digest_matches("abc123", "abc123  /data/model.bin\n"))
        self.assertFalse(digest_matches("abc123", "def456  /data/model.bin\n"))


if __name__ == "__main__":
    unittest.main()
