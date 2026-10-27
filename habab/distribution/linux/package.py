#!/usr/bin/env python3
"""Build a source-bound, deterministic ARM64 installer archive."""
import argparse
import gzip
import hashlib
import io
import importlib.util
import json
import re
import tarfile
from pathlib import Path
import sys


DIST = Path(__file__).resolve().parents[1]
REPO = DIST.parents[1]
candidate_spec = importlib.util.spec_from_file_location(
    "habab_frontend_candidate", REPO / "habab/native-frontend/candidate.py")
frontend_candidate = importlib.util.module_from_spec(candidate_spec)
candidate_spec.loader.exec_module(frontend_candidate)
APP_FILES = (
    "config/configuration.yaml",
    "release.json",
    "opencode-release.json",
    "install-xiaomi-home.py",
    "habab-ha.py",
    "xiaomi-home-release.json",
    "telegram-agent",
    "src/habab_home",
    "src/habab_telegram",
    "src/habab_yandex_speech",
    "src/idigital_sms",
)


def build(source_sha: str, wheel: Path, manifest_path: Path, output: Path,
          requirements_lock: Path | None = None) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("source SHA must be a full lowercase Git SHA-1")
    manifest, validated_wheel, _ = frontend_candidate.load_candidate(manifest_path.parent, source_sha)
    if validated_wheel.resolve() != wheel.resolve():
        raise ValueError("frontend wheel path does not match its candidate manifest")
    release = json.loads((DIST / "release.json").read_text())
    wheel_bytes = wheel.read_bytes()
    digest = hashlib.sha256(wheel_bytes).hexdigest()
    version = manifest.get("wheel_version", "")
    if manifest.get("artifact_sha256") != digest:
        raise ValueError("frontend candidate digest changed after validation")

    payload = DIST / "linux" / "payload"
    entries = {}
    for relative in APP_FILES:
        path = DIST / relative
        for item in ([path] if path.is_file() else sorted(path.rglob("*"))):
            if (item.is_file() and not item.is_symlink()
                    and "__pycache__" not in item.parts and item.suffix != ".pyc"):
                entries[f"payload/app/{item.relative_to(DIST).as_posix()}"] = item.read_bytes()
    entries["payload/app/frontend-release.json"] = json.dumps({
        "version": version, "filename": wheel.name,
        "bytes": len(wheel_bytes), "sha256": digest,
    }, sort_keys=True, indent=2).encode() + b"\n"
    entries[f"payload/app/frontend/{wheel.name}"] = wheel_bytes
    if requirements_lock:
        lock_bytes = requirements_lock.read_bytes()
        if b"homeassistant==" not in lock_bytes or b"--hash=sha256:" not in lock_bytes:
            raise ValueError("requirements lock must pin Home Assistant and include hashes")
        entries["payload/app/requirements.lock"] = lock_bytes
    for name in ("install.py", "manage.py", "update.py"):
        item = DIST / "linux" / name
        entries[f"payload/linux/{item.name}"] = item.read_bytes()
    for name in ("install.sh", "update.sh", "rollback.sh"):
        item = DIST / "linux" / name
        entries[f"payload/linux/{name}"] = item.read_bytes()
    entries["payload/linux/runtime-pins.json"] = (DIST / "linux" / "runtime-pins.json").read_bytes()
    release_manifest = {
        "product_version": release["version"],
        "home_assistant": release["home_assistant"]["version"],
        "frontend": version, "frontend_sha256": digest,
        "opencode": "1.14.21", "node": "24.22.1",
        "architecture": "arm64", "source_sha": source_sha,
    }
    entries["payload/release.json"] = json.dumps(release_manifest, sort_keys=True, indent=2).encode() + b"\n"

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as file:
        with gzip.GzipFile(fileobj=file, mode="wb", mtime=0, filename="") as compressed:
            with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                for name, content in sorted(entries.items()):
                    info = tarfile.TarInfo(name)
                    info.size = len(content)
                    info.mode = 0o755 if name.endswith(".sh") else 0o644
                    info.uid = info.gid = 0
                    info.uname = info.gname = "root"
                    info.mtime = 0
                    archive.addfile(info, io.BytesIO(content))
    return {**release_manifest, "archive": str(output),
            "archive_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "archive_bytes": output.stat().st_size}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--frontend-wheel", required=True, type=Path)
    parser.add_argument("--frontend-manifest", required=True, type=Path)
    parser.add_argument("--requirements-lock", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.source_sha, args.frontend_wheel,
                           args.frontend_manifest, args.output,
                           args.requirements_lock), sort_keys=True))


if __name__ == "__main__":
    main()
