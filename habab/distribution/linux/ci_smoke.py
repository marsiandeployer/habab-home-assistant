#!/usr/bin/env python3
"""Exercise the native installer on a disposable Ubuntu ARM64 Actions VM."""
import argparse
import json
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from update import extract


BASE = "http://127.0.0.1:8123"


def request(path, body=None, token=None, form=False):
    headers = {}
    if body is None:
        data = None
    elif form:
        data = urllib.parse.urlencode(body).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    else:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(
        BASE + path, data=data, headers=headers), timeout=20) as response:
        return json.load(response)


def wait(path, token=None, timeout=420):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return request(path, token=token)
        except (OSError, ValueError):
            time.sleep(3)
    raise TimeoutError(f"Native service did not serve {path}")


def command(*args):
    try:
        return subprocess.run(args, check=True, capture_output=True, text=True, timeout=900)
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"Native smoke command failed: {(error.stderr or '')[-3000:]}") from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--frontend-version", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not __import__("os").environ.get("GITHUB_ACTIONS"):
        parser.error("This smoke test creates an owner and runs only on a disposable Actions VM")
    if subprocess.run(["systemctl", "is-active", "--quiet", "habab-home.service"]).returncode == 0:
        parser.error("Refusing to replace an existing running service")
    try:
        with tempfile.TemporaryDirectory(prefix="habab-native-smoke-") as directory:
            payload = extract(args.package, directory)
            command("sudo", "bash", str(payload / "linux/install.sh"))
        flows = wait("/api/onboarding")
        if not isinstance(flows, list) or not any(row.get("step") == "user" for row in flows):
            raise RuntimeError("Clean native install did not offer owner onboarding")
        client = BASE + "/"
        owner = request("/api/onboarding/users", {
            "client_id": client, "name": "Release smoke", "username": "release-smoke",
            "password": secrets.token_urlsafe(24), "language": "en",
        })
        auth = request("/auth/token", {
            "grant_type": "authorization_code", "code": owner["auth_code"],
            "client_id": client,
        }, form=True)
        token = auth["access_token"]
        config = request("/api/config", token=token)
        status = wait("/api/habab_home/status", token=token)
        if status.get("product") != "Habab Home" or config.get("version") != "2026.10.2":
            raise RuntimeError("Native authenticated product status did not match the release")
        current = Path("/opt/habab-home/current")
        source = json.loads(command("sudo", "cat", str(current / "source.json")).stdout)
        if source.get("source_sha") != args.source_sha:
            raise RuntimeError("Installed source differs from the package source")
        python = str(current / "venv/bin/python")
        frontend = command("sudo", "-u", "habab", python, "-c", "from importlib.metadata import version; print(version('home-assistant-frontend'))").stdout.strip()
        if frontend != args.frontend_version:
            raise RuntimeError("Installed frontend differs from the freshly compiled wheel")
        opencode = command(
            "sudo", "-u", "habab", "env", "HOME=/var/lib/habab-home",
            "XDG_CONFIG_HOME=/var/lib/habab-home/.config",
            "XDG_CACHE_HOME=/var/lib/habab-home/.cache",
            "XDG_DATA_HOME=/var/lib/habab-home/.local/share",
            str(current / "bin/opencode"), "--version",
        ).stdout.strip()
        marker = "/var/lib/habab-home/config/.native-release-smoke"
        command("sudo", "touch", marker)
        command("sudo", "systemctl", "restart", "habab-home.service")
        wait("/api/habab_home/status", token=token)
        request("/api/config", token=token)
        command("sudo", "test", "-f", marker)
        result = {
            "state": "passed", "environment": "Ubuntu 26.04 ARM64 Actions VM",
            "source_sha": args.source_sha, "frontend": frontend, "opencode": opencode,
            "installation": "native systemd, no Docker", "owner_onboarding": "passed",
            "authenticated_product_status": "passed", "restart_persistence": "passed",
            "raspberry_pi_physical": "NOT RUN", "update_rollback": "NOT RUN",
        }
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
    finally:
        subprocess.run(["sudo", "systemctl", "disable", "--now", "habab-home.service"],
                       capture_output=True, timeout=90)


if __name__ == "__main__":
    main()
