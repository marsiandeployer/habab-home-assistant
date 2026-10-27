#!/usr/bin/env python3
"""Run a resource-bounded smoke check against a locally loaded release image."""

import argparse
import json
from pathlib import Path
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


def run(command: list[str], *, timeout: int = 60) -> str:
    result = subprocess.run(command, check=True, capture_output=True, text=True,
                            timeout=timeout)
    return result.stdout.strip()


def request_json(base_url: str, path: str, body=None, token=None, form=False):
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
    request = urllib.request.Request(base_url + path, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def wait_for_json(base_url: str, path: str, timeout: int, token=None):
    deadline = time.monotonic() + timeout
    last_error = "no response"
    while time.monotonic() < deadline:
        try:
            return request_json(base_url, path, token=token)
        except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError) as error:
            last_error = str(error)
        time.sleep(3)
    raise TimeoutError(f"Home Assistant endpoint {path} did not become ready: {last_error}")


def assert_product_status(status):
    if (status.get("product") != "Habab Home" or status.get("version") != "0.3.6"
            or status.get("home_assistant_version") != "2026.10.2"
            or status.get("status") != "running"):
        raise RuntimeError("Authenticated Habab Home status did not match the release")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--platform", choices=("linux/amd64", "linux/arm64"), required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--frontend-version", required=True)
    parser.add_argument("--frontend-sha256", required=True)
    parser.add_argument("--opencode-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--startup-timeout", type=int, default=360)
    args = parser.parse_args()

    architecture = args.platform.split("/", 1)[1]
    release = json.loads(args.opencode_manifest.read_text(encoding="utf-8"))[architecture]
    expected_opencode = str(release["version"])
    container = f"habab-docker-smoke-{architecture}-{uuid.uuid4().hex[:10]}"
    marker = ".habab-docker-release-smoke"
    config_volume = f"habab-docker-smoke-config-{architecture}-{uuid.uuid4().hex[:10]}"
    started = False
    try:
        run(["docker", "volume", "create", config_volume])
        run([
            "docker", "run", "--detach", "--name", container,
            "--pull=never",
            "--platform", args.platform,
            "--memory=2g", "--memory-swap=2g", "--cpus=2", "--pids-limit=256",
            "--publish", "127.0.0.1::8123", "--volume", f"{config_volume}:/config",
            "--env", "TZ=UTC", args.image,
        ])
        started = True
        port = run(["docker", "port", container, "8123/tcp"])
        host_port = port.rsplit(":", 1)[-1]
        base_url = f"http://127.0.0.1:{host_port}"
        flows = wait_for_json(base_url, "/api/onboarding", args.startup_timeout)
        if not isinstance(flows, list) or not any(
            isinstance(flow, dict) and flow.get("step") == "user" for flow in flows
        ):
            raise RuntimeError("Clean Home Assistant install did not offer owner onboarding")

        client_id = base_url + "/"
        owner = request_json(base_url, "/api/onboarding/users", {
            "client_id": client_id,
            "name": "Habab Docker release smoke",
            "username": f"docker-smoke-{uuid.uuid4().hex[:10]}",
            "password": secrets.token_urlsafe(24),
            "language": "en",
        })
        auth_code = owner.get("auth_code") if isinstance(owner, dict) else None
        if not isinstance(auth_code, str) or not auth_code:
            raise RuntimeError("Owner onboarding did not return an authorization code")
        auth = request_json(base_url, "/auth/token", {
            "grant_type": "authorization_code",
            "code": auth_code,
            "client_id": client_id,
        }, form=True)
        access_token = auth.get("access_token") if isinstance(auth, dict) else None
        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError("Owner onboarding did not return an access token")
        config = request_json(base_url, "/api/config", token=access_token)
        if config.get("version") != "2026.10.2":
            raise RuntimeError("Authenticated Home Assistant config did not match Core release")
        status = wait_for_json(base_url, "/api/habab_home/status", args.startup_timeout, token=access_token)
        assert_product_status(status)

        labels = json.loads(run([
            "docker", "image", "inspect", args.image,
            "--format", "{{json .Config.Labels}}",
        ]))
        expected_labels = {
            "org.opencontainers.image.revision": args.source_revision,
            "io.habab.source.dirty": "false",
            "io.habab.frontend.version": args.frontend_version,
            "io.habab.frontend.sha256": args.frontend_sha256,
        }
        for name, expected in expected_labels.items():
            if labels.get(name) != expected:
                raise RuntimeError(f"Image label {name} did not match release manifest")

        frontend_version = run([
            "docker", "exec", container, "python3", "-c",
            "from importlib.metadata import version; print(version('home-assistant-frontend'))",
        ])
        if frontend_version != args.frontend_version:
            raise RuntimeError("Installed frontend package version differs from release manifest")

        import_check = (
            "import importlib; "
            "[importlib.import_module('homeassistant.components.' + name) for name in "
            "('habab_home', 'habab_telegram', 'habab_yandex_speech', 'idigital_sms')]; "
            "print('custom integrations imported')"
        )
        run(["docker", "exec", container, "python3", "-c", import_check], timeout=120)

        opencode_version = run([
            "docker", "exec", container, "/opt/habab-opencode/opencode", "--version",
        ], timeout=60)
        if expected_opencode not in opencode_version:
            raise RuntimeError("Embedded OpenCode version differs from its pinned manifest")

        run(["docker", "exec", container, "sh", "-c",
             f"printf '%s' release-smoke > /config/{marker}"])
        run(["docker", "restart", container], timeout=90)
        # An automatically allocated host port can change when Docker restarts
        # the container. The release Compose file uses a fixed port instead.
        restarted_port = run(["docker", "port", container, "8123/tcp"])
        base_url = f"http://127.0.0.1:{restarted_port.rsplit(':', 1)[-1]}"
        status_after_restart = wait_for_json(
            base_url, "/api/habab_home/status", args.startup_timeout, token=access_token
        )
        assert_product_status(status_after_restart)
        config_after_restart = request_json(base_url, "/api/config", token=access_token)
        if config_after_restart.get("version") != "2026.10.2":
            raise RuntimeError("Owner authentication or Home Assistant API did not survive restart")
        marker_value = run(["docker", "exec", container, "cat", f"/config/{marker}"])
        if marker_value != "release-smoke":
            raise RuntimeError("/config marker did not survive container restart")

        result = {
            "state": "passed",
            "image": args.image,
            "platform": args.platform,
            "source_revision": args.source_revision,
            "frontend_version": frontend_version,
            "custom_integrations": [
                "habab_home", "habab_telegram", "habab_yandex_speech", "idigital_sms",
            ],
            "opencode_version": expected_opencode,
            "owner_onboarding": "passed",
            "authenticated_product_status": "passed",
            "authenticated_status_after_restart": "passed",
            "config_persistence": "passed",
        }
        result_json = json.dumps(result, sort_keys=True)
        if args.output:
            args.output.write_text(result_json + "\n", encoding="utf-8")
        print(result_json)
    except Exception:
        if started:
            logs = subprocess.run(["docker", "logs", "--tail", "80", container],
                                  check=False, capture_output=True, text=True, timeout=30)
            if logs.stdout or logs.stderr:
                print("Container log tail:", file=sys.stderr)
                print((logs.stdout + logs.stderr)[-12000:], file=sys.stderr)
        raise
    finally:
        for command in (
            ["docker", "rm", "--force", container],
            ["docker", "volume", "rm", config_volume],
        ):
            try:
                subprocess.run(command, check=False, capture_output=True,
                               text=True, timeout=90)
            except (OSError, subprocess.TimeoutExpired):
                pass


if __name__ == "__main__":
    main()
