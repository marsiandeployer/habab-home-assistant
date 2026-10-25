#!/usr/bin/env python3
"""Guarded operator entrypoint for the isolated local-voice QA profile."""

from __future__ import annotations

import argparse
from getpass import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import wave
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
PROJECT = "habab-local-voice-qa"
_DOCKER_CONFIG: str | None = None
MODEL = "qwen3:4b-instruct"
MODEL_MANIFEST_DIGEST = "sha256:0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0"
MODEL_BLOB = "sha256:85e4a5b7b8ef0e48af0e8658f5aaab9c2324c76c1641493f4d1e25fce54b18b9"
WHISPER_MODEL_SHA256 = "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671"
PIPER_MODEL_SHA256 = "15fab56e11a097858ee115545d0f697fc2a316c41a291a5362349fb870411b0a"
PINNED_IMAGES = (
    "ghcr.io/home-assistant/home-assistant@sha256:33afb0659d786213264c2211f8d91c5fe87461c56eba6a3a546eb05fc71e89f4",
    "rhasspy/wyoming-whisper@sha256:ba6fcb6056ebe237d15a325381763a80af2fc8fcedaa04f6a714a7375eb20d80",
    "rhasspy/wyoming-piper@sha256:7d39aafac409c2b6b09d999ed04d84c311d632e558b35baa60fad6aac722af7a",
    "ollama/ollama@sha256:dea72d1e22270fe095b68fdf756d2b489d615643c5a2eaebbc5f569181e322d8",
)
GIB = 1024**3
_GIN_CHAT_SUCCESS = re.compile(r'\|\s*(\d{3})\s*\|.*\|\s*POST\s+"/api/chat"\s*$')

# Conservative amd64 planning inputs, in bytes, from read-only registry/model
# metadata. Assume 3x compressed image layers after unpack plus one compressed
# copy during pulls, model data, a second Ollama model-sized temporary copy,
# 2 GiB metadata/runtime allowance, and a separate 10 GiB free-space reserve.
COMPRESSED_IMAGES = 4_699_395_047
MODEL_DATA = 3_044_033_499
OLLAMA_TEMP_COPY = 2_497_280_480
EXTRA_ALLOWANCE = 2 * GIB
RESERVE = 10 * GIB
MIN_FREE_BYTES = max(
    COMPRESSED_IMAGES * 4
    + MODEL_DATA
    + OLLAMA_TEMP_COPY
    + EXTRA_ALLOWANCE
    + RESERVE,
    36 * GIB,
)


class GuardError(RuntimeError):
    pass


def parse_mode(argv: list[str] | None = None) -> str:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--prepare", action="store_true", help="pull pinned images and warm the local model volumes")
    modes.add_argument("--run-offline", action="store_true", help="start the isolated profile with WAN blocked")
    modes.add_argument("--run-synthetic-turn", action="store_true", help="run one synthetic Assist turn against the isolated profile")
    modes.add_argument("--stop-offline", action="store_true", help="stop this QA Compose project and keep its volumes")
    args = parser.parse_args(argv)
    if args.prepare:
        return "prepare"
    if args.run_offline:
        return "run-offline"
    if args.run_synthetic_turn:
        return "run-synthetic-turn"
    return "stop-offline"


def check_disk_budget(free_bytes: int, required_bytes: int = MIN_FREE_BYTES) -> None:
    if free_bytes < required_bytes:
        required_gib = math.ceil(required_bytes / GIB)
        free_gib = free_bytes / GIB
        raise GuardError(
            f"Docker storage has {free_gib:.1f} GiB free; this profile requires "
            f"at least {required_gib} GiB free before downloads."
        )


def is_local_endpoint(host: str) -> bool:
    return host.startswith(("unix://", "npipe://"))


def is_local_filesystem(fs_type: str) -> bool:
    # Fail closed on FUSE, network and unknown types; overlay is common for a local Docker data root.
    return fs_type.lower() in {"ext4", "xfs", "btrfs", "zfs", "f2fs", "overlay"}


def check_loopback_port_available(port: int) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError as err:
        raise GuardError(f"Loopback port {port} is unavailable; no service was started.") from err
    finally:
        probe.close()


def model_blob_digest(manifest: dict[str, object]) -> str | None:
    layers = manifest.get("layers")
    if not isinstance(layers, list):
        return None
    for layer in layers:
        if isinstance(layer, dict) and layer.get("mediaType") == "application/vnd.ollama.image.model":
            digest = layer.get("digest")
            return digest if isinstance(digest, str) else None
    return None


def digest_matches(expected: str, command_output: str) -> bool:
    return command_output.split(maxsplit=1)[0] == expected if command_output.strip() else False


def verify_remote_model_pin() -> None:
    request = Request(
        "https://registry.ollama.ai/v2/library/qwen3/manifests/4b-instruct",
        headers={"Accept": "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"},
    )
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read(65536)
    except Exception as err:
        raise GuardError("Could not verify the pinned Ollama model manifest before preparation.") from err
    if hashlib.sha256(raw).hexdigest() != MODEL_MANIFEST_DIGEST.removeprefix("sha256:"):
        raise GuardError("The Ollama model tag no longer resolves to the reviewed manifest digest.")
    try:
        manifest = json.loads(raw)
    except ValueError as err:
        raise GuardError("The Ollama model registry returned invalid manifest metadata.") from err
    if model_blob_digest(manifest) != MODEL_BLOB:
        raise GuardError("The Ollama model manifest does not contain the reviewed model blob.")


def _run(args: list[str], *, timeout: int = 120, capture: bool = False) -> subprocess.CompletedProcess[str]:
    env = None
    if args[0] == "docker":
        if _DOCKER_CONFIG is None:
            raise GuardError("Docker commands require the runner's temporary empty config directory.")
        args = ["docker", "--config", _DOCKER_CONFIG, "--context", "default", *args[1:]]
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    try:
        return subprocess.run(
            args,
            cwd=ROOT,
            check=True,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as err:
        # Do not echo command output; container logs can include model prompts.
        raise GuardError(f"Command failed during {args[0]} operation.") from err


def _compose(*args: str, profiles: tuple[str, ...] = ("offline",), timeout: int = 120, capture: bool = False) -> subprocess.CompletedProcess[str]:
    command = [
        "docker",
        "compose",
        "--project-directory",
        str(ROOT),
        "--env-file",
        str(ROOT / "compose.env"),
        "--project-name",
        PROJECT,
        "--file",
        str(ROOT / "compose.yaml"),
    ]
    for profile in profiles:
        command.extend(("--profile", profile))
    command.extend(args)
    return _run(command, timeout=timeout, capture=capture)


def _ensure_fixture_scope() -> None:
    expected = ROOT / "fixture" / "configuration.yaml"
    compose_path = ROOT / "compose.yaml"
    if not compose_path.is_file() or not expected.is_file():
        raise GuardError("QA Compose file or isolated fixture is missing.")
    config = expected.read_text(encoding="utf-8")
    compose = compose_path.read_text(encoding="utf-8")
    if "light.qa_virtual_light" in config or "!secret" in config or "secrets.yaml" in config:
        raise GuardError("Fixture contains a forbidden external entity or secret reference.")
    if "unique_id: qa_virtual_light" not in config:
        raise GuardError("Fixture virtual light is missing.")
    required_markers = (
        "./fixture/configuration.yaml:/config/configuration.yaml:ro",
        '"127.0.0.1:18444:18444"',
        "internal: true",
        "com.habab.qa.owner: voice20_luna",
    )
    if any(marker not in compose for marker in required_markers) or any(image not in compose for image in PINNED_IMAGES):
        raise GuardError("Compose must use the isolated fixture, loopback-only UI, internal runtime network, and pinned images.")
    if "network_mode: host" in compose or "/root/.ssh" in compose:
        raise GuardError("Compose contains a forbidden host or owner-runtime mount.")


def _docker_storage() -> tuple[Path, int, str]:
    context = _run(["docker", "context", "show"], capture=True).stdout.strip()
    info = _run(["docker", "context", "inspect", context], capture=True).stdout
    try:
        endpoint = json.loads(info)[0]["Endpoints"]["docker"]["Host"]
    except (ValueError, KeyError, IndexError, TypeError) as err:
        raise GuardError("Could not verify that the selected Docker endpoint is local.") from err
    if not is_local_endpoint(endpoint):
        raise GuardError("Remote Docker contexts are disabled for this QA profile.")

    root_dir = Path(_run(["docker", "info", "--format", "{{.DockerRootDir}}"], capture=True).stdout.strip())
    if not root_dir.is_dir():
        raise GuardError("Docker storage directory is unavailable for a local disk check.")
    fs = _run(["findmnt", "-n", "-o", "FSTYPE", "-T", str(root_dir)], capture=True).stdout.strip()
    if not is_local_filesystem(fs):
        raise GuardError("Docker storage filesystem type is not in the supported local-filesystem allowlist.")
    return root_dir, shutil.disk_usage(root_dir).free, fs


def _ensure_no_existing_containers(profiles: tuple[str, ...]) -> None:
    result = _compose("ps", "--all", "--quiet", profiles=profiles, capture=True)
    if result.stdout.strip():
        raise GuardError("This QA Compose project already has containers; inspect them before continuing.")


def _verify_project_owner() -> None:
    container_ids = _run(
        ["docker", "ps", "--all", "--quiet", "--filter", f"label=com.docker.compose.project={PROJECT}"],
        capture=True,
    ).stdout.split()
    for container_id in container_ids:
        raw = _run(["docker", "inspect", "--type", "container", "--format", "{{json .Config.Labels}}", container_id], capture=True).stdout
        labels = json.loads(raw)
        if labels.get("com.habab.qa.owner") != "voice20_luna" or labels.get("com.habab.qa.profile") != "local-voice-offline-20261026":
            raise GuardError("A container with this Compose project name has a different owner label.")
    network_ids = _run(
        ["docker", "network", "ls", "--quiet", "--filter", f"label=com.docker.compose.project={PROJECT}"],
        capture=True,
    ).stdout.split()
    for network_id in network_ids:
        raw = _run(["docker", "network", "inspect", "--format", "{{json .Labels}}", network_id], capture=True).stdout
        labels = json.loads(raw)[0]
        if labels.get("com.habab.qa.owner") != "voice20_luna" or labels.get("com.habab.qa.profile") != "local-voice-offline-20261026":
            raise GuardError("A network with this Compose project name has a different owner label.")
    volume_names = _run(
        ["docker", "volume", "ls", "--quiet", "--filter", f"label=com.docker.compose.project={PROJECT}"],
        capture=True,
    ).stdout.split()
    for volume_name in volume_names:
        raw = _run(["docker", "volume", "inspect", "--format", "{{json .Labels}}", volume_name], capture=True).stdout
        labels = json.loads(raw)[0]
        if labels.get("com.habab.qa.owner") != "voice20_luna" or labels.get("com.habab.qa.profile") != "local-voice-offline-20261026":
            raise GuardError("A volume with this Compose project name has a different owner label.")


def _require_offline_stack_running() -> None:
    expected = {"home-assistant", "whisper", "piper", "ollama"}
    result = _compose("ps", "--services", "--status", "running", profiles=("offline",), capture=True)
    services = result.stdout.split()
    if set(services) != expected or len(services) != len(expected):
        raise GuardError("The owned offline profile is not fully running; no services were started by this mode.")


def _validate_synthetic_wav(path: Path) -> int:
    try:
        with wave.open(str(path), "rb") as wav:
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            sample_rate = wav.getframerate()
            frames = wav.getnframes()
    except (OSError, EOFError, wave.Error):
        raise GuardError("Synthetic audio generation did not produce a valid WAV.") from None
    if channels != 1 or sample_width != 2 or not 8_000 <= sample_rate <= 48_000 or not 0 < frames <= sample_rate * 10:
        raise GuardError("Synthetic WAV must be mono PCM16 and no longer than ten seconds.")
    return sample_rate


def _create_synthetic_wav(path: Path) -> None:
    espeak = shutil.which("espeak-ng")
    if espeak is None:
        raise GuardError("espeak-ng is required to create the synthetic Russian input.")
    try:
        completed = subprocess.run(
            [espeak, "-w", str(path), "-v", "ru", "-s", "150", "Включи тестовую лампу"],
            cwd=ROOT,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise GuardError("Synthetic Russian audio generation failed.") from None
    if completed.returncode != 0:
        raise GuardError("Synthetic Russian audio generation failed.")
    _validate_synthetic_wav(path)


def count_successful_chat_requests(log_text: str) -> int:
    """Count only successful Ollama Gin access-log records for the chat route."""
    count = 0
    for line in log_text.splitlines():
        match = _GIN_CHAT_SUCCESS.search(line)
        if match is not None and match.group(1) == "200":
            count += 1
    return count


def _ollama_chat_request_count() -> int:
    result = _compose(
        "logs",
        "--no-color",
        "--timestamps",
        "--tail",
        "200",
        "ollama",
        profiles=("offline",),
        capture=True,
    )
    if len(result.stdout.splitlines()) >= 200:
        raise GuardError("Ollama access-log window exceeded the safe in-memory bound.")
    return count_successful_chat_requests(result.stdout)


def _prepare_service(service: str, timeout: int = 900) -> None:
    _compose("up", "--detach", "--wait", "--wait-timeout", str(timeout), service, profiles=("prepare",), timeout=timeout + 30)


def _remove_prepare_containers() -> None:
    _compose("rm", "--stop", "--force", profiles=("prepare",))


def _verify_ollama_model(service: str) -> None:
    result = _compose(
        "exec",
        "--no-TTY",
        service,
        "cat",
        "/root/.ollama/models/manifests/registry.ollama.ai/library/qwen3/4b-instruct",
        profiles=("prepare",) if service == "prepare-ollama" else ("offline",),
        capture=True,
    )
    try:
        manifest = json.loads(result.stdout)
    except ValueError as err:
        raise GuardError("The expected Qwen model manifest is missing or unreadable.") from err
    if model_blob_digest(manifest) != MODEL_BLOB:
        raise GuardError("The pulled Qwen model blob does not match the reviewed digest.")


def _verify_voice_models(profile: str, *, check_whisper: bool = True, check_piper: bool = True) -> None:
    service_by_profile = {
        "prepare": ("prepare-whisper", "prepare-piper"),
        "offline": ("whisper", "piper"),
    }
    whisper, piper = service_by_profile[profile]
    if check_whisper:
        candidates = _compose(
            "exec",
            "--no-TTY",
            whisper,
            "find",
            "/data",
            "-name",
            "model.bin",
            "-print",
            "-quit",
            profiles=(profile,),
            capture=True,
        ).stdout.strip().splitlines()
        if not candidates:
            raise GuardError("The pinned Whisper model file is missing.")
        whisper_sum = _compose("exec", "--no-TTY", whisper, "sha256sum", candidates[0], profiles=(profile,), capture=True).stdout
        if not digest_matches(WHISPER_MODEL_SHA256, whisper_sum):
            raise GuardError("The cached Whisper model file does not match the reviewed digest.")
    if check_piper:
        piper_sum = _compose(
            "exec",
            "--no-TTY",
            piper,
            "sha256sum",
            "/data/ru_RU-denis-medium.onnx",
            profiles=(profile,),
            capture=True,
        ).stdout
        if not digest_matches(PIPER_MODEL_SHA256, piper_sum):
            raise GuardError("The cached Piper voice file does not match the reviewed digest.")


def prepare() -> None:
    _ensure_fixture_scope()
    _verify_project_owner()
    _ensure_no_existing_containers(("prepare", "offline"))
    root_dir, free, fs = _docker_storage()
    check_disk_budget(free)
    print(f"Docker storage: {root_dir} ({fs}); free space meets the {math.ceil(MIN_FREE_BYTES / GIB)} GiB floor.")
    verify_remote_model_pin()
    for image in PINNED_IMAGES:
        _run(["docker", "pull", image], timeout=900)
    try:
        _prepare_service("prepare-whisper")
        _verify_voice_models("prepare", check_piper=False)
        _remove_prepare_containers()
        _prepare_service("prepare-piper")
        _verify_voice_models("prepare", check_whisper=False)
        _remove_prepare_containers()
        _prepare_service("prepare-ollama")
        deadline = time.monotonic() + 120
        while True:
            try:
                _compose("exec", "--no-TTY", "prepare-ollama", "ollama", "list", profiles=("prepare",), timeout=10, capture=True)
                break
            except GuardError:
                if time.monotonic() >= deadline:
                    raise GuardError("Ollama did not become ready during preparation.")
                time.sleep(2)
        _compose("exec", "--no-TTY", "prepare-ollama", "ollama", "pull", MODEL, profiles=("prepare",), timeout=3600)
        _verify_ollama_model("prepare-ollama")
        _compose("exec", "--no-TTY", "prepare-ollama", "ollama", "run", MODEL, "Ответь одним словом: готов", profiles=("prepare",), timeout=600)
    finally:
        _remove_prepare_containers()
        _compose("down", profiles=("prepare",), timeout=120)
    print("Preparation finished. No Home Assistant instance was started.")


def run_offline() -> None:
    _ensure_fixture_scope()
    _ensure_no_existing_containers(("prepare", "offline"))
    _verify_project_owner()
    _, free, _ = _docker_storage()  # Reject remote Docker and remote volume storage before starting.
    check_disk_budget(free, RESERVE)
    check_loopback_port_available(18444)
    try:
        _compose(
            "up",
            "--detach",
            "--pull",
            "never",
            "--wait",
            "--wait-timeout",
            "180",
            "home-assistant",
            "whisper",
            "piper",
            "ollama",
            profiles=("offline",),
            timeout=210,
        )
        _verify_voice_models("offline")
        _verify_ollama_model("ollama")
    except GuardError:
        _compose("down", profiles=("offline",), timeout=120)
        raise
    print("Offline stack is running at http://127.0.0.1:18444. Create a temporary HA admin account there, then run --run-synthetic-turn.")


def run_synthetic_turn() -> None:
    _ensure_fixture_scope()
    _verify_project_owner()
    _require_offline_stack_running()
    _, free, _ = _docker_storage()
    check_disk_budget(free, RESERVE)
    _verify_voice_models("offline")
    _verify_ollama_model("ollama")

    try:
        from fixture_setup import FixtureSetupError, cleanup_fixture, setup_fixture
        from offline_turn import OfflineTurnError, run_offline_turn
    except ImportError:
        raise GuardError("The local-voice QA helper modules are unavailable.") from None

    if not sys.stdin.isatty():
        raise GuardError("Run --run-synthetic-turn from an interactive terminal so the token can be entered without echo.")
    access_token = getpass("Temporary HA admin access token (input hidden): ")
    if not access_token:
        raise GuardError("An access token is required for this one synthetic turn.")

    fixture = None
    try:
        fixture = setup_fixture("http://127.0.0.1:18444", access_token)
        with tempfile.TemporaryDirectory(prefix="habab-local-voice-audio-") as temp_dir:
            wav_path = Path(temp_dir) / "synthetic-command.wav"
            _create_synthetic_wav(wav_path)
            chat_count_before = _ollama_chat_request_count()
            turn = run_offline_turn("http://127.0.0.1:18444", access_token, fixture, wav_path)
            if turn.status != "completed" or turn.model_id != MODEL:
                raise GuardError("Synthetic Assist turn did not complete through the selected local model route.")
            chat_count_after = _ollama_chat_request_count()
            if chat_count_after < chat_count_before:
                raise GuardError("Ollama access-log window changed during the synthetic turn.")
            chat_request_count = chat_count_after - chat_count_before
            if chat_request_count < 1:
                raise GuardError("No successful Ollama POST /api/chat was recorded during the synthetic turn.")
            if turn.virtual_light_state != "on":
                raise GuardError("The synthetic Assist turn did not leave the QA virtual light on.")
    except FixtureSetupError as err:
        raise GuardError(f"HA fixture setup failed ({err.code}).") from None
    except OfflineTurnError as err:
        raise GuardError(f"Synthetic Assist turn failed ({err.code}).") from None
    except GuardError:
        raise
    except Exception:
        raise GuardError("Synthetic Assist turn failed with an unexpected local harness error.") from None
    finally:
        try:
            if fixture is not None:
                try:
                    cleanup_fixture("http://127.0.0.1:18444", access_token, fixture)
                except FixtureSetupError as err:
                    raise GuardError(f"HA fixture cleanup failed ({err.code}).") from None
                except Exception:
                    raise GuardError("HA fixture cleanup failed with an unexpected local helper error.") from None
        finally:
            access_token = ""

    print(
        f"Synthetic turn completed; successful Ollama POST /api/chat count={chat_request_count}; "
        f"selected model={MODEL}; virtual light=on. No transcript or audio was retained."
    )


def stop_offline() -> None:
    _docker_storage()
    _verify_project_owner()
    _compose("down", profiles=("offline",), timeout=120)
    print("Offline containers and network stopped; named model/config volumes were kept.")


def main(argv: list[str] | None = None) -> int:
    try:
        mode = parse_mode(argv)
        global _DOCKER_CONFIG
        with tempfile.TemporaryDirectory(prefix="habab-local-voice-docker-config-") as docker_config:
            _DOCKER_CONFIG = docker_config
            if mode == "prepare":
                prepare()
            elif mode == "run-offline":
                run_offline()
            elif mode == "run-synthetic-turn":
                run_synthetic_turn()
            else:
                stop_offline()
    except GuardError as err:
        print(f"QA guard: {err}", file=sys.stderr)
        return 2
    finally:
        _DOCKER_CONFIG = None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
