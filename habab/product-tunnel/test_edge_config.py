import base64
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import edge_config
import manage_home


def fake_key(seed: int) -> str:
    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes([seed]) * 32
    return "ssh-ed25519 " + base64.b64encode(blob).decode() + " test-fixture-only"


class EdgeConfigTest(unittest.TestCase):
    def test_edge_host_routes_to_matching_generated_backend_and_challenge(self) -> None:
        home_id = "issue8pilot"
        port = 32808
        host, edge = edge_config.render(home_id, port)
        backend = manage_home.render(home_id, host, port, fake_key(8), "edge")
        backend_nginx = backend[manage_home.NGINX_DIR / f"habab-product-{home_id}.conf"].decode()

        self.assertEqual(host, "home-issue8pilot.remote.habab.ru")
        self.assertEqual(edge.count(f"server_name {host};"), 2)
        self.assertIn("location ^~ /.well-known/acme-challenge/", edge)
        self.assertIn("root /var/www/habab.ru;", edge)
        self.assertIn(f"/etc/letsencrypt/live/{host}/fullchain.pem", edge)
        self.assertIn(f"/etc/letsencrypt/live/{host}/privkey.pem", edge)
        self.assertIn("proxy_pass http://95.217.227.164:80;", edge)
        self.assertIn("proxy_set_header Host $host;", edge)
        self.assertIn("proxy_set_header X-Forwarded-Proto https;", edge)
        self.assertIn("proxy_set_header X-Forwarded-For $remote_addr;", edge)
        self.assertIn(f"server_name {host};", backend_nginx)
        self.assertIn(f"proxy_pass http://127.0.0.1:{port};", backend_nginx)
        self.assertIn("proxy_set_header Connection $connection_upgrade;", backend_nginx)

    def test_staged_pilot_files_match_current_generators(self) -> None:
        home_id = "issue8pilot"
        port = 32808
        host, edge = edge_config.render(home_id, port)
        backend = manage_home.render(home_id, host, port, fake_key(8), "edge")
        staged = Path(__file__).with_name("staging") / home_id

        self.assertEqual((staged / "edge-vhost.conf").read_text(), edge)
        self.assertEqual(
            (staged / "vm104-backend-vhost.conf").read_bytes(),
            backend[manage_home.NGINX_DIR / f"habab-product-{home_id}.conf"],
        )

    def test_rejects_invalid_home_and_port(self) -> None:
        with self.assertRaises(edge_config.EdgeConfigError):
            edge_config.render("pilot; include /tmp/x", 32808)
        with self.assertRaises(edge_config.EdgeConfigError):
            edge_config.render("issue8pilot", 22)

    def test_existing_product_state_rejects_duplicate_relay_port(self) -> None:
        script = Path(manage_home.__file__)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "relay"
            first = [
                sys.executable, str(script), "provision", "--home-id", "first-home",
                "--host", "home-first-home.remote.habab.ru", "--port", "32808",
                "--public-key", fake_key(1), "--sandbox-root", str(root), "--apply",
            ]
            subprocess.run(first, check=True, capture_output=True, text=True)

            duplicate = [
                sys.executable, str(script), "provision", "--home-id", "issue8pilot",
                "--host", "home-issue8pilot.remote.habab.ru", "--port", "32808",
                "--public-key", fake_key(2), "--sandbox-root", str(root),
            ]
            rejected = subprocess.run(duplicate, capture_output=True, text=True)

        self.assertEqual(rejected.returncode, 2)
        self.assertIn("host or loopback port is already assigned to home first-home", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
