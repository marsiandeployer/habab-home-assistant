#!/usr/bin/env python3
"""Assemble checksums and provenance for a manually built release candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile


_SHA = re.compile(r"\A[0-9a-f]{40,64}\Z")
_TARGETS = ("windows", "linux-amd64", "linux-arm64", "android")
_ARTIFACT_NAMES = {
    "windows": "candidate-windows",
    "linux-amd64": "candidate-linux-amd64",
    "linux-arm64": "candidate-linux-arm64",
    "android": "candidate-android",
}
_WINDOWS_PACKAGE_FILES = {
    "start-home-assistant.bat",
    "start-xiaomi-home.bat",
    "support_tunnel.bat",
}


def _digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            size += len(block)
            digest.update(block)
    return digest.hexdigest(), size


def verify_windows_package(directory: Path, source_tree: Path, source_revision: str) -> dict:
    metadata = json.loads((directory / "windows-package.json").read_text(encoding="utf-8"))
    filename = metadata.get("filename")
    if not isinstance(filename, str) or Path(filename).name != filename:
        raise ValueError("Windows package metadata has an invalid filename")
    archive_path = directory / filename
    digest, size = _digest(archive_path)
    if digest != metadata.get("sha256") or size != metadata.get("bytes"):
        raise ValueError("Windows package checksum or size does not match its metadata")

    members = metadata.get("files")
    if not isinstance(members, list) or members != sorted(set(members)) or "release.json" not in members:
        raise ValueError("Windows package member manifest is invalid")
    if any(not isinstance(name, str) or name.startswith("/") or "\\" in name
           or ".." in Path(name).parts for name in members):
        raise ValueError("Windows package member path is unsafe")
    distribution = source_tree / "habab/distribution"
    release_data = json.loads((distribution / "release.json").read_text(encoding="utf-8"))
    release_data.update({"source_revision": source_revision, "source_dirty": "false"})
    release_bytes = (json.dumps(release_data, indent=2) + "\n").encode("utf-8")
    with zipfile.ZipFile(archive_path) as package:
        if package.testzip() is not None:
            raise ValueError("Windows package ZIP failed CRC readback")
        if sorted(package.namelist()) != members:
            raise ValueError("Windows package members differ from its allowlist")
        embedded_release = json.loads(package.read("release.json"))
        if embedded_release.get("source_revision") != source_revision or embedded_release.get("source_dirty") != "false":
            raise ValueError("Windows package does not embed the selected clean source")
        for name in members:
            if name == "release.json":
                expected = release_bytes
            else:
                source = distribution / name
                if not source.is_file() or source.is_symlink():
                    raise ValueError(f"Windows package member is not a regular source file: {name}")
                expected = source.read_bytes()
                if name.endswith(".bat"):
                    expected = expected.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            if package.read(name) != expected:
                raise ValueError(f"Windows package member differs from selected source: {name}")

    for launcher in _WINDOWS_PACKAGE_FILES:
        path = directory / launcher
        if not path.is_file():
            raise ValueError(f"Canonical Windows builder output is missing: {launcher}")
        content = path.read_text(encoding="ascii")
        if filename not in content or digest not in content:
            raise ValueError(f"Windows bootstrap does not pin the package checksum: {launcher}")
    return {"filename": filename, "sha256": digest, "bytes": size}


def assemble(root: Path, source_revision: str, android_state: str,
             source_tree: Path | None = None) -> dict:
    if not _SHA.fullmatch(source_revision):
        raise ValueError("source revision must be a full Git SHA")
    if android_state not in {"blocked", "built"}:
        raise ValueError("Android state must be blocked or built")

    root = root.resolve()
    source_tree = source_tree.resolve() if source_tree else None
    targets = {}
    files = []
    for target in _TARGETS:
        directory = root / target
        status_path = directory / "status.json"
        if not status_path.is_file():
            raise ValueError(f"Missing {target} status readback")
        status = json.loads(status_path.read_text(encoding="utf-8"))
        if status.get("source_revision") != source_revision:
            raise ValueError(f"{target} was built from another source revision")
        if status.get("source_dirty") is not False:
            raise ValueError(f"{target} does not prove a clean source tree")
        if status.get("state") != ("blocked" if target == "android" and android_state == "blocked" else "built"):
            raise ValueError(f"{target} status does not match requested candidate state")
        if target == "windows":
            if source_tree is None or status.get("kind") != "installer-package":
                raise ValueError("Windows target must be the canonical installer package")
            package = verify_windows_package(directory, source_tree, source_revision)
            if status.get("filename") != package["filename"]:
                raise ValueError("Windows target status does not match the packaged ZIP")
        if target == "android" and android_state == "blocked" and not status.get("reason"):
            raise ValueError("Blocked Android status needs a reason")

        target_files = []
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path == status_path:
                continue
            relative = path.relative_to(root).as_posix()
            digest, size = _digest(path)
            record = {"artifact": _ARTIFACT_NAMES[target], "path": relative,
                      "sha256": digest, "bytes": size}
            target_files.append(record)
            files.append(record)
        if status["state"] == "built" and not target_files:
            raise ValueError(f"{target} has no artifact files")
        targets[target] = {**status, "artifact": _ARTIFACT_NAMES[target]}

    return {
        "schema": 1,
        "source_revision": source_revision,
        "source_dirty": False,
        "targets": targets,
        "artifacts": files,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Downloaded target artifacts")
    parser.add_argument("--source-tree", type=Path, required=True, help="Clean checkout at the selected revision")
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--android-state", choices=("blocked", "built"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = assemble(args.root, args.source_revision, args.android_state, args.source_tree)
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"candidate manifest written: {args.output}")


if __name__ == "__main__":
    main()
