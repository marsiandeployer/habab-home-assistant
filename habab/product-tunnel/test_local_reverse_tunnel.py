"""Local-only OpenSSH acceptance for the generated per-home reverse-forward policy."""

from __future__ import annotations

import base64
import os
import pwd
import random
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

import manage_home


SSH = shutil.which("ssh")
SSHD = shutil.which("sshd")
SSH_KEYGEN = shutil.which("ssh-keygen")


def _free_product_port(exclude: set[int] | None = None) -> int:
    excluded = exclude or set()
    candidates = list(range(32000, 40000))
    random.shuffle(candidates)
    for port in candidates:
        if port in excluded:
            continue
        with socket.socket() as listener:
            try:
                listener.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free loopback port in the product tunnel range")


@unittest.skipUnless(
    os.geteuid() == 0 and SSH and SSHD and SSH_KEYGEN,
    "requires root and OpenSSH client/server/keygen for an isolated loopback sshd",
)
class LocalReverseTunnelTest(unittest.TestCase):
    def test_generated_key_opens_only_its_loopback_reverse_listener(self) -> None:
        home_id = "localtest"
        relay_port = _free_product_port()
        denied_port = _free_product_port({relay_port})
        account = pwd.getpwuid(os.geteuid()).pw_name

        with tempfile.TemporaryDirectory(
            prefix="habab-product-tunnel-test-", dir=Path.home()
        ) as directory:
            root = Path(directory)
            identity = root / "client_key"
            server_key = root / "host_key"
            subprocess.run([SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-f", str(identity)],
                           check=True, capture_output=True, text=True)
            subprocess.run([SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-f", str(server_key)],
                           check=True, capture_output=True, text=True)

            client_public_key = identity.with_suffix(".pub").read_text(encoding="ascii").strip()
            key_blob = client_public_key.split()[1]
            decoded = base64.b64decode(key_blob, validate=True)
            self.assertTrue(decoded.startswith(b"\x00\x00\x00\x0bssh-ed25519"))
            home_host = f"home-{home_id}.remote.habab.ru"
            generated = manage_home.render(home_id, home_host, relay_port, client_public_key, "edge")

            key_file = root / "authorized_keys"
            key_file.write_bytes(generated[manage_home.KEY_DIR / home_id])
            key_file.chmod(0o600)
            match_config = generated[manage_home.SSH_DIR / f"80-habab-product-{home_id}.conf"].decode()
            match_config = match_config.replace(f"habab-home-{home_id}", account)
            match_config = match_config.replace(
                f"/etc/habab-product-tunnel/keys/{home_id}", str(key_file)
            )
            include = root / "product-match.conf"
            include.write_text(match_config, encoding="utf-8")

            sshd_port = _free_product_port({relay_port, denied_port})
            config = root / "sshd_config"
            config.write_text(
                f"Port {sshd_port}\n"
                "ListenAddress 127.0.0.1\n"
                f"HostKey {server_key}\n"
                f"PidFile {root / 'sshd.pid'}\n"
                "UsePAM no\n"
                "PermitRootLogin yes\n"
                "PubkeyAuthentication yes\n"
                "PasswordAuthentication no\n"
                "KbdInteractiveAuthentication no\n"
                f"AllowUsers {account}\n"
                "AllowTcpForwarding no\n"
                "GatewayPorts no\n"
                "PermitOpen none\n"
                "PermitTunnel no\n"
                "AllowAgentForwarding no\n"
                "X11Forwarding no\n"
                "PermitTTY no\n"
                f"Include {include}\n",
                encoding="utf-8",
            )

            parsed = subprocess.run([SSHD, "-t", "-f", str(config)], capture_output=True, text=True)
            self.assertEqual(parsed.returncode, 0, parsed.stderr)
            effective = subprocess.run(
                [SSHD, "-T", "-f", str(config), "-C",
                 f"user={account},host=localhost,addr=127.0.0.1"],
                check=True, capture_output=True, text=True,
            ).stdout
            for directive in (
                "allowtcpforwarding remote",
                f"permitlisten 127.0.0.1:{relay_port}",
                "permitopen none",
                "gatewayports no",
                "maxsessions 0",
                "forcecommand /usr/bin/false",
            ):
                self.assertIn(directive, effective)

            known_hosts = root / "known_hosts"
            host_key_parts = server_key.with_suffix(".pub").read_text(encoding="ascii").split()
            known_hosts.write_text(
                f"[127.0.0.1]:{sshd_port} {host_key_parts[0]} {host_key_parts[1]}\n",
                encoding="ascii",
            )

            fixture = socket.socket()
            fixture.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            fixture.bind(("127.0.0.1", 0))
            fixture.listen(1)
            fixture.settimeout(0.2)
            fixture_port = fixture.getsockname()[1]
            stop_fixture = threading.Event()

            def serve_fixture() -> None:
                while not stop_fixture.is_set():
                    try:
                        connection, _ = fixture.accept()
                    except TimeoutError:
                        continue
                    except OSError:
                        return
                    with connection:
                        connection.sendall(b"reverse-tunnel-fixture-ok\n")

            fixture_thread = threading.Thread(target=serve_fixture, daemon=True)
            fixture_thread.start()

            log = (root / "sshd.log").open("w", encoding="utf-8")
            server = subprocess.Popen(
                [SSHD, "-D", "-e", "-f", str(config)],
                stdout=subprocess.DEVNULL, stderr=log, start_new_session=True,
            )
            client: subprocess.Popen[str] | None = None
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if server.poll() is not None:
                        self.fail(f"isolated sshd exited early; log: {log.name}")
                    try:
                        with socket.create_connection(("127.0.0.1", sshd_port), timeout=0.2):
                            break
                    except OSError:
                        time.sleep(0.05)
                else:
                    self.fail("isolated sshd did not start on its loopback port")

                client_command = [
                    SSH, "-F", "/dev/null", "-N", "-T", "-p", str(sshd_port),
                    "-i", str(identity),
                    "-o", "BatchMode=yes",
                    "-o", "IdentitiesOnly=yes",
                    "-o", "IdentityAgent=none",
                    "-o", "PasswordAuthentication=no",
                    "-o", "KbdInteractiveAuthentication=no",
                    "-o", "StrictHostKeyChecking=yes",
                    "-o", f"UserKnownHostsFile={known_hosts}",
                    "-o", "GlobalKnownHostsFile=/dev/null",
                    "-o", "ExitOnForwardFailure=yes",
                    "-o", "ConnectTimeout=3",
                    "-R", f"127.0.0.1:{relay_port}:127.0.0.1:{fixture_port}",
                    f"{account}@127.0.0.1",
                ]
                client = subprocess.Popen(client_command, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.PIPE, text=True)
                response = b""
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline and not response:
                    if client.poll() is not None:
                        client_error = client.stderr.read() if client.stderr else ""
                        log.flush()
                        self.fail(
                            f"SSH client exited before opening the forward: {client_error}; "
                            f"sshd log: {Path(log.name).read_text(encoding='utf-8')}"
                        )
                    try:
                        with socket.create_connection(("127.0.0.1", relay_port), timeout=0.4) as forwarded:
                            forwarded.settimeout(1)
                            response = forwarded.recv(128)
                    except OSError:
                        time.sleep(0.05)
                self.assertEqual(response, b"reverse-tunnel-fixture-ok\n")

                denied = subprocess.run(
                    client_command[:client_command.index("-R")] + [
                        "-R", f"127.0.0.1:{denied_port}:127.0.0.1:{fixture_port}",
                        f"{account}@127.0.0.1",
                    ],
                    capture_output=True, text=True, timeout=6,
                )
                self.assertNotEqual(denied.returncode, 0, "a second reverse-forward port was accepted")
            finally:
                if client is not None and client.poll() is None:
                    client.terminate()
                    try:
                        client.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        client.kill()
                        client.wait(timeout=2)
                if client is not None and client.stderr is not None:
                    client.stderr.close()
                if server.poll() is None:
                    os.killpg(server.pid, signal.SIGTERM)
                    try:
                        server.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(server.pid, signal.SIGKILL)
                        server.wait(timeout=3)
                stop_fixture.set()
                fixture.close()
                fixture_thread.join(timeout=2)
                log.close()
                self.assertIsNotNone(server.poll(), "temporary sshd was left running")


if __name__ == "__main__":
    unittest.main()
