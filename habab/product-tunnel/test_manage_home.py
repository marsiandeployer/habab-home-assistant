import base64
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


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

    def provision(self, root: Path, home_id: str, port: int, public_key: str) -> None:
        self.run_cli(
            "provision", "--home-id", home_id,
            "--host", f"home-{home_id}.remote.habab.ru",
            "--port", str(port), "--public-key", public_key,
            "--sandbox-root", str(root), "--apply",
        )

    def test_idempotency_isolation_and_revoke(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sandbox"
            alpha_key_value = fake_key(1, "operator@alpha")
            beta_key_value = fake_key(2, "operator@beta")
            self.provision(root, "alpha", 32041, alpha_key_value)
            self.provision(root, "beta", 32042, beta_key_value)
            alpha_key = root / "etc/habab-product-tunnel/keys/alpha"
            beta_key = root / "etc/habab-product-tunnel/keys/beta"
            alpha_before = alpha_key.read_bytes()
            beta_before = beta_key.read_bytes()

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


if __name__ == "__main__":
    unittest.main()
