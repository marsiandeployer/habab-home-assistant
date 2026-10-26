#!/usr/bin/env python3
"""Assemble checksums and provenance for a manually built release candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import tarfile
import zipfile

_DISTRIBUTION_MODULES = Path(__file__).resolve().parents[1] / "distribution"
if str(_DISTRIBUTION_MODULES) not in sys.path:
    sys.path.insert(0, str(_DISTRIBUTION_MODULES))
from windows_package_files import resolve_windows_package_source


_SHA = re.compile(r"\A[0-9a-f]{40,64}\Z")
_OCI_DIGEST = re.compile(r"\Asha256:([0-9a-f]{64})\Z")
_OCI_IDENTITY_LABELS = (
    "org.opencontainers.image.revision",
    "io.habab.source.dirty",
)
_OCI_JSON_LIMIT = 16 * 1024 * 1024
_TARGETS = ("windows", "linux-amd64", "linux-arm64", "android")
_TARGET_SCOPES = {
    "all": _TARGETS,
    "windows": ("windows",),
    "linux": ("linux-amd64", "linux-arm64"),
    "android": ("android",),
}
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


def _read_oci_json(archive: tarfile.TarFile, name: str) -> tuple[bytes, dict]:
    matches = [member for member in archive.getmembers() if member.name == name]
    if len(matches) != 1 or not matches[0].isreg() or matches[0].size > _OCI_JSON_LIMIT:
        raise ValueError(f"OCI archive has an invalid {name}")
    source = archive.extractfile(matches[0])
    if source is None:
        raise ValueError(f"OCI archive is missing {name}")
    with source:
        raw = source.read(_OCI_JSON_LIMIT + 1)
    if len(raw) != matches[0].size or len(raw) > _OCI_JSON_LIMIT:
        raise ValueError(f"OCI archive has an invalid {name} size")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"OCI archive has invalid JSON in {name}") from error
    if not isinstance(value, dict):
        raise ValueError(f"OCI archive has invalid JSON in {name}")
    return raw, value


def _read_oci_blob(archive: tarfile.TarFile, digest: str) -> bytes:
    match = _OCI_DIGEST.fullmatch(digest) if isinstance(digest, str) else None
    if match is None:
        raise ValueError("OCI descriptor has an invalid SHA-256 digest")
    name = f"blobs/sha256/{match.group(1)}"
    matches = [member for member in archive.getmembers() if member.name == name]
    if len(matches) != 1 or not matches[0].isreg() or matches[0].size > _OCI_JSON_LIMIT:
        raise ValueError(f"OCI archive has an invalid blob: {name}")
    source = archive.extractfile(matches[0])
    if source is None:
        raise ValueError(f"OCI archive is missing blob: {name}")
    with source:
        raw = source.read(_OCI_JSON_LIMIT + 1)
    if len(raw) != matches[0].size or len(raw) > _OCI_JSON_LIMIT:
        raise ValueError(f"OCI archive has an invalid blob size: {name}")
    if hashlib.sha256(raw).hexdigest() != match.group(1):
        raise ValueError(f"OCI blob digest does not match descriptor: {name}")
    return raw


def _verify_oci_layer(archive: tarfile.TarFile, descriptor: object) -> None:
    """Stream-check one layer blob without loading a potentially large image into memory."""
    if not isinstance(descriptor, dict):
        raise ValueError("OCI manifest has an invalid layer descriptor")
    digest = descriptor.get("digest")
    match = _OCI_DIGEST.fullmatch(digest) if isinstance(digest, str) else None
    size = descriptor.get("size")
    if match is None or type(size) is not int or size < 0:
        raise ValueError("OCI layer descriptor has an invalid digest or size")
    name = f"blobs/sha256/{match.group(1)}"
    matches = [member for member in archive.getmembers() if member.name == name]
    if len(matches) != 1 or not matches[0].isreg() or matches[0].size != size:
        raise ValueError(f"OCI archive has an invalid layer: {name}")
    source = archive.extractfile(matches[0])
    if source is None:
        raise ValueError(f"OCI archive is missing layer: {name}")
    hasher = hashlib.sha256()
    bytes_read = 0
    with source:
        while chunk := source.read(1024 * 1024):
            bytes_read += len(chunk)
            if bytes_read > size:
                raise ValueError(f"OCI layer size exceeds descriptor: {name}")
            hasher.update(chunk)
    if bytes_read != size or hasher.hexdigest() != match.group(1):
        raise ValueError(f"OCI layer digest or size does not match descriptor: {name}")


def _valid_oci_digest(value: object) -> bool:
    return isinstance(value, str) and _OCI_DIGEST.fullmatch(value) is not None


def verify_linux_image(image_path: Path, platform: str, source_revision: str) -> dict:
    """Read the selected OCI platform manifest and prove its provenance labels."""
    if platform not in {"linux/amd64", "linux/arm64"}:
        raise ValueError("Linux OCI platform must be linux/amd64 or linux/arm64")
    if not _SHA.fullmatch(source_revision):
        raise ValueError("source revision must be a full Git SHA")

    architecture = platform.split("/", 1)[1]
    try:
        with tarfile.open(image_path, mode="r:") as archive:
            _, layout = _read_oci_json(archive, "oci-layout")
            if layout.get("imageLayoutVersion") != "1.0.0":
                raise ValueError("OCI image layout version is unsupported")
            _, index = _read_oci_json(archive, "index.json")
            descriptors = index.get("manifests")
            if index.get("schemaVersion") != 2 or not isinstance(descriptors, list):
                raise ValueError("OCI image index is invalid")
            matching = [descriptor for descriptor in descriptors
                        if isinstance(descriptor, dict)
                        and isinstance(descriptor.get("platform"), dict)
                        and descriptor["platform"].get("os") == "linux"
                        and descriptor["platform"].get("architecture") == architecture]
            if len(matching) != 1:
                raise ValueError(f"OCI index must contain one {platform} manifest")

            manifest_digest = matching[0].get("digest")
            manifest_raw = _read_oci_blob(archive, manifest_digest)
            if matching[0].get("size") != len(manifest_raw):
                raise ValueError("OCI manifest size does not match index descriptor")
            manifest = json.loads(manifest_raw)
            if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 2:
                raise ValueError("OCI platform manifest is invalid")
            layers = manifest.get("layers")
            if not isinstance(layers, list) or not layers:
                raise ValueError("OCI platform manifest has no layers")
            for layer in layers:
                _verify_oci_layer(archive, layer)
            config_descriptor = manifest.get("config")
            if not isinstance(config_descriptor, dict):
                raise ValueError("OCI platform manifest has no config descriptor")
            config_digest = config_descriptor.get("digest")
            config_raw = _read_oci_blob(archive, config_digest)
            if config_descriptor.get("size") != len(config_raw):
                raise ValueError("OCI config size does not match manifest descriptor")
            config = json.loads(config_raw)
    except (OSError, tarfile.TarError) as error:
        raise ValueError(f"Could not read Linux OCI archive: {error}") from error

    if not isinstance(config, dict):
        raise ValueError("OCI image config is invalid")
    if config.get("os") != "linux" or config.get("architecture") != architecture:
        raise ValueError("OCI image config platform does not match selected platform")
    image_config = config.get("config")
    labels = image_config.get("Labels") if isinstance(image_config, dict) else None
    if not isinstance(labels, dict):
        raise ValueError("OCI image config has no labels")
    verified_labels = {name: labels.get(name) for name in _OCI_IDENTITY_LABELS}
    if verified_labels["org.opencontainers.image.revision"] != source_revision:
        raise ValueError("OCI image revision label does not match selected source")
    if verified_labels["io.habab.source.dirty"] != "false":
        raise ValueError("OCI image dirty-source label is not false")
    return {
        "platform": platform,
        "manifest_digest": manifest_digest,
        "config_digest": config_digest,
        "verified_labels": verified_labels,
    }


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
                source = resolve_windows_package_source(distribution, name)
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
             source_tree: Path | None = None, target_scope: str = "all",
             workflow_identity: dict | None = None) -> dict:
    if not _SHA.fullmatch(source_revision):
        raise ValueError("source revision must be a full Git SHA")
    if android_state not in {"blocked", "built"}:
        raise ValueError("Android state must be blocked or built")
    if target_scope not in _TARGET_SCOPES:
        raise ValueError("target scope must be all, windows, linux, or android")
    identity_fields = ("workflow_revision", "workflow_run_id", "workflow_run_attempt")
    if not isinstance(workflow_identity, dict) or any(
            not isinstance(workflow_identity.get(field), str) or not workflow_identity[field]
            for field in identity_fields):
        raise ValueError("expected workflow identity is missing or invalid")

    root = root.resolve()
    source_tree = source_tree.resolve() if source_tree else None
    targets = {}
    files = []
    for target in _TARGET_SCOPES[target_scope]:
        directory = root / target
        status_path = directory / "status.json"
        if not status_path.is_file():
            raise ValueError(f"Missing {target} status readback")
        status = json.loads(status_path.read_text(encoding="utf-8"))
        if status.get("source_revision") != source_revision:
            raise ValueError(f"{target} was built from another source revision")
        if status.get("source_dirty") is not False:
            raise ValueError(f"{target} does not prove a clean source tree")
        if any(status.get(field) != workflow_identity[field] for field in identity_fields):
            raise ValueError(f"{target} workflow identity does not match the aggregate job")
        if status.get("state") != ("blocked" if target == "android" and android_state == "blocked" else "built"):
            raise ValueError(f"{target} status does not match requested candidate state")
        if target == "windows":
            if source_tree is None or status.get("kind") != "installer-package":
                raise ValueError("Windows target must be the canonical installer package")
            package = verify_windows_package(directory, source_tree, source_revision)
            if status.get("filename") != package["filename"]:
                raise ValueError("Windows target status does not match the packaged ZIP")
        if target in {"linux-amd64", "linux-arm64"}:
            expected_platform = "linux/amd64" if target == "linux-amd64" else "linux/arm64"
            oci_image = status.get("oci_image")
            if status.get("platform") != expected_platform or not isinstance(oci_image, dict):
                raise ValueError(f"{target} status has no verified OCI image identity")
            if oci_image.get("platform") != expected_platform:
                raise ValueError(f"{target} OCI identity has the wrong platform")
            if not _valid_oci_digest(oci_image.get("manifest_digest")):
                raise ValueError(f"{target} OCI manifest digest is invalid")
            if not _valid_oci_digest(oci_image.get("config_digest")):
                raise ValueError(f"{target} OCI config digest is invalid")
            labels = oci_image.get("verified_labels")
            if not isinstance(labels, dict):
                raise ValueError(f"{target} OCI image labels were not verified")
            if labels.get("org.opencontainers.image.revision") != source_revision:
                raise ValueError(f"{target} OCI revision label does not match selected source")
            if labels.get("io.habab.source.dirty") != "false":
                raise ValueError(f"{target} OCI dirty-source label is not false")
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
    parser.add_argument("--root", type=Path, help="Downloaded target artifacts")
    parser.add_argument("--source-tree", type=Path, help="Clean checkout at the selected revision")
    parser.add_argument("--source-revision")
    parser.add_argument("--target-scope", choices=tuple(_TARGET_SCOPES), default="all")
    parser.add_argument("--android-state", choices=("blocked", "built"))
    parser.add_argument("--workflow-revision")
    parser.add_argument("--workflow-run-id")
    parser.add_argument("--workflow-run-attempt")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify-linux-image", type=Path,
                        help="Verify OCI identity labels and write a Linux target status")
    parser.add_argument("--platform", choices=("linux/amd64", "linux/arm64"))
    parser.add_argument("--status-output", type=Path)
    args = parser.parse_args()
    if args.verify_linux_image is not None:
        if not args.source_revision or not args.platform or not args.status_output:
            parser.error("--verify-linux-image requires --source-revision, --platform, and --status-output")
        identity = verify_linux_image(args.verify_linux_image, args.platform, args.source_revision)
        status = {
            "state": "built",
            "source_revision": args.source_revision,
            "source_dirty": False,
            "platform": args.platform,
            "oci_image": identity,
        }
        args.status_output.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"verified OCI config {identity['config_digest']} for {args.platform}")
        return
    if args.root is None or args.source_tree is None or not args.source_revision \
            or args.android_state is None or not args.workflow_revision or not args.workflow_run_id \
            or not args.workflow_run_attempt or args.output is None:
        parser.error("assembly requires --root, --source-tree, --source-revision, --android-state, "
                     "workflow identity, and --output")
    manifest = assemble(args.root, args.source_revision, args.android_state,
                        args.source_tree, args.target_scope, {
                            "workflow_revision": args.workflow_revision,
                            "workflow_run_id": args.workflow_run_id,
                            "workflow_run_attempt": args.workflow_run_attempt,
                        })
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"candidate manifest written: {args.output}")


if __name__ == "__main__":
    main()
