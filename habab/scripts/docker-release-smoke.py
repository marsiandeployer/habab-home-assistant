#!/usr/bin/env python3
"""Run a resource-bounded smoke check against a locally loaded release image."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid


def run(command: list[str], *, timeout: int = 60) -> str:
    result = subprocess.run(command, check=True, capture_output=True, text=True,
                            timeout=timeout)
    return result.stdout.strip()


def wait_for_onboarding(url: str, timeout: int) -> list:
    deadline = time.monotonic() + timeout
    last_error = "no response"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status != 200:
                    last_error = f"HTTP {response.status}"
                else:
                    payload = json.loads(response.read(1024 * 1024))
                    if not isinstance(payload, list):
                        raise ValueError("onboarding response is not a list")
                    return payload
        except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError) as error:
            last_error = str(error)
        time.sleep(3)
    raise TimeoutError(f"Home Assistant onboarding endpoint did not become ready: {last_error}")


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
        url = f"http://127.0.0.1:{host_port}/api/onboarding"
        flows = wait_for_onboarding(url, args.startup_timeout)
        if not flows or not any(isinstance(flow, dict) for flow in flows):
            raise RuntimeError("Home Assistant returned no onboarding flow")

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
        flows_after_restart = wait_for_onboarding(url, args.startup_timeout)
        if not flows_after_restart:
            raise RuntimeError("Onboarding endpoint returned no flow after restart")
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
            "onboarding_before_restart": "http_200",
            "onboarding_after_restart": "http_200",
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
