"""End-to-end local TLS -> product SSH reverse-forward acceptance."""

from __future__ import annotations

import base64
import os
import pwd
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
OPENSSL = shutil.which("openssl")
NGINX = shutil.which("nginx")
CURL = shutil.which("curl")


def _free_loopback_port(exclude: set[int] | None = None) -> int:
    excluded = exclude or set()
    for _ in range(64):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        if port not in excluded:
            return port
    raise RuntimeError("could not reserve a temporary loopback port")


def _free_product_port(exclude: set[int] | None = None) -> int:
    excluded = exclude or set()
    for port in range(32000, 40000):
        if port in excluded:
            continue
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free loopback port in the product range")


@unittest.skipUnless(
    os.geteuid() == 0 and all((SSH, SSHD, SSH_KEYGEN, OPENSSL, NGINX, CURL)),
    "requires root and OpenSSH/nginx/openssl/curl for a disposable loopback test",
)
class LocalEdgeHopTest(unittest.TestCase):
    def test_staged_fragments_match_manager_local_tls_mode(self) -> None:
        home_id = "issue8pilot"
        host = f"home-{home_id}.remote.habab.ru"
        relay_port = 32808
        key_blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes([8]) * 32
        public_key = "ssh-ed25519 " + base64.b64encode(key_blob).decode() + " synthetic-only"
        generated = manage_home.render(home_id, host, relay_port, public_key, "local")
        staged = Path(__file__).with_name("staging") / home_id / "edge-hop"

        self.assertEqual(
            (staged / f"sshd-home-{home_id}.conf").read_bytes(),
            generated[manage_home.SSH_DIR / f"80-habab-product-{home_id}.conf"],
        )
        self.assertEqual(
            (staged / f"nginx-home-{home_id}.conf").read_bytes(),
            generated[manage_home.NGINX_DIR / f"habab-product-{home_id}.conf"],
        )

    def test_exact_home_tls_vhost_reaches_loopback_forward_only(self) -> None:
        home_id = "issue8pilot"
        host = f"home-{home_id}.remote.habab.ru"
        relay_port = _free_product_port()
        sshd_port = _free_loopback_port({relay_port})
        nginx_port = _free_loopback_port({relay_port, sshd_port})
        account = pwd.getpwuid(os.geteuid()).pw_name
        fixture_body = b"edge-hop-fixture-ok\n"

        with tempfile.TemporaryDirectory(prefix="habab-edge-hop-test-", dir=Path.home()) as directory:
            root = Path(directory)
            client_key = root / "client_key"
            host_key = root / "host_key"
            certificate = root / "certificate.pem"
            certificate_key = root / "certificate-key.pem"
            subprocess.run(
                [SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-f", str(client_key)],
                check=True, capture_output=True, text=True,
            )
            subprocess.run(
                [SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)],
                check=True, capture_output=True, text=True,
            )
            subprocess.run(
                [OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                 "-subj", f"/CN={host}", "-addext", f"subjectAltName=DNS:{host}",
                 "-keyout", str(certificate_key), "-out", str(certificate)],
                check=True, capture_output=True, text=True,
            )

            public_key = client_key.with_suffix(".pub").read_text(encoding="ascii").strip()
            generated = manage_home.render(home_id, host, relay_port, public_key, "local")
            authorized_keys = root / "authorized_keys"
            authorized_keys.write_bytes(generated[manage_home.KEY_DIR / home_id])
            authorized_keys.chmod(0o600)

            match = generated[manage_home.SSH_DIR / f"80-habab-product-{home_id}.conf"].decode()
            match = match.replace(f"habab-home-{home_id}", account)
            match = match.replace(
                f"/etc/habab-product-tunnel/keys/{home_id}", str(authorized_keys)
            )
            match_file = root / "product-match.conf"
            match_file.write_text(match, encoding="utf-8")

            sshd_config = root / "sshd_config"
            sshd_config.write_text(
                f"Port {sshd_port}\nListenAddress 127.0.0.1\nHostKey {host_key}\n"
                f"PidFile {root / 'sshd.pid'}\nUsePAM no\nPermitRootLogin yes\n"
                "PubkeyAuthentication yes\nPasswordAuthentication no\n"
                "KbdInteractiveAuthentication no\n"
                f"AllowUsers {account}\nAllowTcpForwarding no\nGatewayPorts no\n"
                "PermitOpen none\nPermitTunnel no\nAllowAgentForwarding no\n"
                "X11Forwarding no\nPermitTTY no\n"
                f"Include {match_file}\n",
                encoding="utf-8",
            )
            checked = subprocess.run([SSHD, "-t", "-f", str(sshd_config)],
                                     capture_output=True, text=True)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            effective = subprocess.run(
                [SSHD, "-T", "-f", str(sshd_config), "-C",
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

            host_parts = host_key.with_suffix(".pub").read_text(encoding="ascii").split()
            known_hosts = root / "known_hosts"
            known_hosts.write_text(
                f"[127.0.0.1]:{sshd_port} {host_parts[0]} {host_parts[1]}\n",
                encoding="ascii",
            )

            fixture = socket.socket()
            fixture.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            fixture.bind(("127.0.0.1", 0))
            fixture.listen(8)
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
                        connection.settimeout(1)
                        request = bytearray()
                        try:
                            while b"\r\n\r\n" not in request and len(request) < 8192:
                                chunk = connection.recv(1024)
                                if not chunk:
                                    break
                                request.extend(chunk)
                        except OSError:
                            continue
                        response = (
                            b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: "
                            + str(len(fixture_body)).encode("ascii")
                            + b"\r\nConnection: close\r\n\r\n"
                            + fixture_body
                        )
                        try:
                            connection.sendall(response)
                        except OSError:
                            pass

            fixture_thread = threading.Thread(target=serve_fixture, daemon=True)
            fixture_thread.start()

            nginx_fragment = generated[
                manage_home.NGINX_DIR / f"habab-product-{home_id}.conf"
            ].decode()
            nginx_fragment = nginx_fragment.replace(
                "listen 443 ssl;", f"listen 127.0.0.1:{nginx_port} ssl;"
            )
            nginx_fragment = nginx_fragment.replace(
                f"/etc/letsencrypt/live/{host}/fullchain.pem", str(certificate)
            )
            nginx_fragment = nginx_fragment.replace(
                f"/etc/letsencrypt/live/{host}/privkey.pem", str(certificate_key)
            )
            self.assertIn(f"proxy_pass http://127.0.0.1:{relay_port};", nginx_fragment)
            server_fragment = root / "edge-hop.conf"
            server_fragment.write_text(nginx_fragment, encoding="utf-8")
            nginx_config = root / "nginx.conf"
            nginx_config.write_text(
                f"worker_processes 1;\npid {root / 'nginx.pid'};\nerror_log stderr;\n"
                "events { worker_connections 64; }\n"
                "http {\n"
                "  map $http_upgrade $connection_upgrade { default upgrade; '' close; }\n"
                f"  include {server_fragment};\n"
                "}\n",
                encoding="utf-8",
            )
            nginx_test = subprocess.run(
                [NGINX, "-t", "-c", str(nginx_config), "-p", f"{root}/"],
                capture_output=True, text=True,
            )
            self.assertEqual(nginx_test.returncode, 0, nginx_test.stderr)

            sshd_log = (root / "sshd.log").open("w", encoding="utf-8")
            sshd = subprocess.Popen(
                [SSHD, "-D", "-e", "-f", str(sshd_config)],
                stdout=subprocess.DEVNULL, stderr=sshd_log, start_new_session=True,
            )
            ssh_client: subprocess.Popen[str] | None = None
            nginx_process: subprocess.Popen[str] | None = None
            try:
                self._wait_listening(sshd, sshd_port, sshd_log)
                ssh_client = subprocess.Popen(
                    [
                        SSH, "-F", "/dev/null", "-N", "-T", "-p", str(sshd_port),
                        "-i", str(client_key), "-o", "BatchMode=yes",
                        "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
                        "-o", "PasswordAuthentication=no",
                        "-o", "KbdInteractiveAuthentication=no",
                        "-o", "StrictHostKeyChecking=yes",
                        "-o", f"UserKnownHostsFile={known_hosts}",
                        "-o", "GlobalKnownHostsFile=/dev/null",
                        "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=3",
                        "-R", f"127.0.0.1:{relay_port}:127.0.0.1:{fixture_port}",
                        f"{account}@127.0.0.1",
                    ],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                    start_new_session=True,
                )
                self._wait_forward(ssh_client, relay_port)

                nginx_process = subprocess.Popen(
                    [NGINX, "-g", "daemon off;", "-c", str(nginx_config), "-p", f"{root}/"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                self._wait_listening(nginx_process, nginx_port)
                result = subprocess.run(
                    [
                        CURL, "--silent", "--show-error", "--noproxy", "*",
                        "--cacert", str(certificate), "--max-time", "4",
                        "--resolve", f"{host}:{nginx_port}:127.0.0.1",
                        "--write-out", "\n%{http_code}",
                        f"https://{host}:{nginx_port}/api/",
                    ],
                    check=True, capture_output=True, text=True, timeout=6,
                )
                body, status = result.stdout.rsplit("\n", 1)
                self.assertEqual(status, "200")
                self.assertEqual(body, fixture_body.decode())

                alternate_port = _free_loopback_port({relay_port, sshd_port, nginx_port})
                rejected = subprocess.run(
                    [
                        SSH, "-F", "/dev/null", "-N", "-T", "-p", str(sshd_port),
                        "-i", str(client_key), "-o", "BatchMode=yes",
                        "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
                        "-o", "PasswordAuthentication=no",
                        "-o", "KbdInteractiveAuthentication=no",
                        "-o", "StrictHostKeyChecking=yes",
                        "-o", f"UserKnownHostsFile={known_hosts}",
                        "-o", "GlobalKnownHostsFile=/dev/null",
                        "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=3",
                        "-R", f"127.0.0.1:{alternate_port}:127.0.0.1:{fixture_port}",
                        f"{account}@127.0.0.1",
                    ],
                    capture_output=True, text=True, timeout=6,
                )
                self.assertNotEqual(rejected.returncode, 0)
            finally:
                self._stop_process(nginx_process)
                self._stop_process(ssh_client)
                if ssh_client is not None and ssh_client.stderr is not None:
                    ssh_client.stderr.close()
                self._stop_process(sshd)
                sshd_log.close()
                stop_fixture.set()
                fixture.close()
                fixture_thread.join(timeout=2)

        for port in (relay_port, sshd_port, nginx_port):
            with socket.socket() as probe:
                probe.settimeout(0.2)
                self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0)

    def _wait_listening(self, process: subprocess.Popen[str], port: int, log=None) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                if log is not None:
                    log.flush()
                    diagnostic = Path(log.name).read_text(encoding="utf-8")
                else:
                    diagnostic = ""
                self.fail(f"temporary listener exited early: {diagnostic}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.05)
        self.fail(f"temporary loopback listener did not start on {port}")

    def _wait_forward(self, client: subprocess.Popen[str], port: int) -> None:
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if client.poll() is not None:
                error = client.stderr.read() if client.stderr else ""
                self.fail(f"temporary SSH client exited before forwarding: {error}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                    return
            except OSError:
                time.sleep(0.05)
        self.fail("temporary product reverse-forward did not open")

    def _stop_process(self, process: subprocess.Popen[str] | None) -> None:
        if process is None or process.poll() is not None:
            return
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
