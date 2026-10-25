import base64
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import manage_home


SCRIPT = Path(__file__).with_name("manage_home.py")


def fake_key(seed: int, comment: str) -> str:
    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes([seed]) * 32
    return "ssh-ed25519 " + base64.b64encode(blob).decode() + " " + comment


class ManageHomeTest(unittest.TestCase):
    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )

    def provision(self, root: Path, home_id: str, port: int, public_key: str,
                  tls_termination: str | None = None) -> None:
        arguments = [
            "provision", "--home-id", home_id,
            "--host", f"home-{home_id}.remote.habab.ru",
            "--port", str(port), "--public-key", public_key,
            "--sandbox-root", str(root), "--apply",
        ]
        if tls_termination is not None:
            arguments.extend(["--tls-termination", tls_termination])
        self.run_cli(*arguments)

    def test_idempotency_isolation_and_revoke(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sandbox"
            alpha_key_value = fake_key(1, "operator@alpha")
            beta_key_value = fake_key(2, "operator@beta")
            self.provision(root, "alpha", 32041, alpha_key_value)
            self.provision(root, "beta", 32042, beta_key_value, tls_termination="local")
            alpha_key = root / "etc/habab-product-tunnel/keys/alpha"
            beta_key = root / "etc/habab-product-tunnel/keys/beta"
            alpha_nginx = root / "etc/nginx/conf.d/habab-product-alpha.conf"
            beta_nginx = root / "etc/nginx/conf.d/habab-product-beta.conf"
            alpha_before = alpha_key.read_bytes()
            beta_before = beta_key.read_bytes()
            self.assertIn(b"listen 80;", alpha_nginx.read_bytes())
            self.assertNotIn(b"ssl_certificate", alpha_nginx.read_bytes())
            self.assertIn(b"listen 443 ssl;", beta_nginx.read_bytes())
            self.assertIn(b"ssl_certificate ", beta_nginx.read_bytes())
            alpha_manifest = (root / "var/lib/habab-product-tunnel/homes/alpha.json").read_text()
            beta_manifest = (root / "var/lib/habab-product-tunnel/homes/beta.json").read_text()
            self.assertIn('"manifest_version": 2', alpha_manifest)
            self.assertIn('"tls_termination": "edge"', alpha_manifest)
            self.assertIn('"tls_termination": "local"', beta_manifest)

            preview = self.run_cli(
                "provision", "--home-id", "alpha",
                "--host", "home-alpha.remote.habab.ru", "--port", "32041",
                "--public-key", alpha_key_value, "--sandbox-root", str(root),
            )
            self.assertIn("No changes.", preview.stdout)
            self.assertEqual(alpha_before, alpha_key.read_bytes())

            revoke_preview = self.run_cli("revoke", "--home-id", "alpha", "--sandbox-root", str(root))
            self.assertIn("Preview only", revoke_preview.stdout)
            self.assertTrue(alpha_key.exists())

            duplicate = fake_key(1, "different-comment")
            with self.assertRaises(subprocess.CalledProcessError) as rejected:
                self.run_cli(
                    "provision", "--home-id", "beta",
                    "--host", "home-beta.remote.habab.ru", "--port", "32042",
                    "--public-key", duplicate, "--sandbox-root", str(root),
                )
            self.assertIn("public key is already assigned to home alpha", rejected.exception.stderr)
            self.assertEqual(beta_before, beta_key.read_bytes())

            self.run_cli("revoke", "--home-id", "alpha", "--sandbox-root", str(root), "--apply")
            self.assertFalse(alpha_key.exists())
            self.assertFalse((root / "var/lib/habab-product-tunnel/homes/alpha.json").exists())
            self.assertEqual(beta_before, beta_key.read_bytes())
            self.assertTrue((root / "var/lib/habab-product-tunnel/homes/beta.json").exists())

    def test_apply_failure_rolls_back_and_preserves_concurrent_change(self) -> None:
        home_id = "rollback"
        key = fake_key(3, "operator@rollback")
        host = f"home-{home_id}.remote.habab.ru"
        files = manage_home.render(home_id, host, 32043, key)
        manifest_path = manage_home._manifest_path(home_id)
        manifest = manage_home._manifest(home_id, host, 32043, key, "edge", files)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "rollback"
            original_write = manage_home._atomic_write
            writes = 0

            def fail_second_write(path: Path, content: bytes, mode: int) -> None:
                nonlocal writes
                writes += 1
                if writes == 2:
                    raise OSError("fixture write failure")
                original_write(path, content, mode)

            with patch.object(manage_home, "_atomic_write", side_effect=fail_second_write):
                with self.assertRaisesRegex(manage_home.ManageError, "rolled back 1 path"):
                    manage_home._apply(root, files, manifest_path, manifest)

            self.assertTrue(all(not (root / relative).exists() for relative in files))
            self.assertFalse((root / manifest_path).exists())

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "concurrent"
            first_path = root / next(iter(files))
            original_write = manage_home._atomic_write
            writes = 0

            def change_first_then_fail(path: Path, content: bytes, mode: int) -> None:
                nonlocal writes
                writes += 1
                if writes == 2:
                    first_path.write_bytes(b"concurrent change")
                    raise OSError("fixture write failure")
                original_write(path, content, mode)

            with patch.object(manage_home, "_atomic_write", side_effect=change_first_then_fail):
                with self.assertRaisesRegex(manage_home.ManageError, "rollback stopped.*changed concurrently"):
                    manage_home._apply(root, files, manifest_path, manifest)

            self.assertEqual(first_path.read_bytes(), b"concurrent change")
            self.assertFalse((root / manifest_path).exists())
            self.assertTrue(all(not (root / relative).exists() for relative in list(files)[1:]))


if __name__ == "__main__":
    unittest.main()
