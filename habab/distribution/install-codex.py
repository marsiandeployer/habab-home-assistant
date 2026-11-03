#!/usr/bin/env python3
"""Install official, checksum-pinned Codex CLI and its required sibling tools."""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import platform
import tarfile
import urllib.request

MAX_ARCHIVE_BYTES = 130_000_000
MAX_BINARY_BYTES = 450_000_000
TARGETS = {"amd64": "x86_64-unknown-linux-musl", "arm64": "aarch64-unknown-linux-musl"}
REQUIRED_FILES = ("codex", "codex-code-mode-host", "codex-exec-server")


def load_release(release_file: Path, arch: str) -> dict:
    if arch not in TARGETS:
        raise ValueError("Unsupported target architecture")
    release = json.loads(release_file.read_text(encoding="utf-8")).get(arch)
    if not isinstance(release, dict) or release.get("version") != "0.166.0":
        raise ValueError("Missing pinned Codex release for target architecture")
    target = TARGETS[arch]
    if release.get("target") != target or set(release.get("artifacts", {})) != set(REQUIRED_FILES):
        raise ValueError("Invalid pinned Codex artifact set")
    for name, artifact in release["artifacts"].items():
        expected_asset = f"{name}-{target}.tar.gz"
        expected_url = f"https://github.com/openai/codex/releases/download/rust-v0.166.0/{expected_asset}"
        digest = artifact.get("sha256", "")
        if (artifact.get("asset") != expected_asset
                or artifact.get("url", expected_url) != expected_url
                or type(artifact.get("bytes")) is not int or artifact["bytes"] <= 0
                or artifact["bytes"] > MAX_ARCHIVE_BYTES
                or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
            raise ValueError("Invalid pinned official Codex artifact metadata: " + name)
    return release


def _read_artifact(name: str, record: dict, archive_dir: Path | None, opener) -> bytes:
    cache_path = archive_dir / record["asset"] if archive_dir is not None else None
    if cache_path is not None and cache_path.is_file():
        data = cache_path.read_bytes()
    else:
        url = f"https://github.com/openai/codex/releases/download/rust-v0.166.0/{record['asset']}"
        with opener(url, timeout=120) as response:
            data = response.read(record["bytes"] + 1)
        if len(data) != record["bytes"]:
            raise ValueError("Codex archive size mismatch: " + name)
        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_suffix(cache_path.suffix + ".partial")
            temporary.write_bytes(data)
            temporary.replace(cache_path)
    if len(data) != record["bytes"]:
        raise ValueError("Cached Codex archive size mismatch: " + name)
    if hashlib.sha256(data).hexdigest() != record["sha256"]:
        raise ValueError("Codex archive SHA-256 mismatch: " + name)
    return data


def install(release_file: Path, output_dir: Path, arch: str, archive_dir: Path | None = None,
            opener=urllib.request.urlopen) -> dict:
    release = load_release(release_file, arch)
    output_dir.mkdir(parents=True, exist_ok=True)
    target_files = {name: output_dir / name for name in REQUIRED_FILES}
    stamp = output_dir / "release.json"
    try:
        saved = json.loads(stamp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    current_files = {}
    executable_files = True
    for name, path in target_files.items():
        if path.is_file():
            current_files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            executable_files = executable_files and bool(path.stat().st_mode & 0o111)
    digest_sidecar = output_dir / "codex.sha256"
    try:
        sidecar_digest = digest_sidecar.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        sidecar_digest = None
    if (saved.get("release") == release and saved.get("files") == current_files
            and set(current_files) == set(REQUIRED_FILES)
            and executable_files
            and sidecar_digest == current_files.get("codex")):
        return {"arch": arch, "version": release["version"], "sha256": current_files["codex"]}

    binaries = {}
    for name in REQUIRED_FILES:
        record = release["artifacts"][name]
        archive_data = _read_artifact(name, record, archive_dir, opener)
        expected_member = record["asset"][:-len(".tar.gz")]
        with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:gz") as bundle:
            matches = [member for member in bundle.getmembers() if member.name == expected_member]
            if len(matches) != 1:
                raise ValueError("Official Codex archive has no unique expected executable: " + name)
            member = matches[0]
            normalized = PurePosixPath(member.name)
            if (not member.isfile() or member.size <= 0 or member.size > MAX_BINARY_BYTES
                    or normalized.is_absolute() or ".." in normalized.parts):
                raise ValueError("Invalid executable in official Codex archive: " + name)
            source = bundle.extractfile(member)
            if source is None:
                raise ValueError("Codex executable could not be read: " + name)
            binary = source.read(MAX_BINARY_BYTES + 1)
            if len(binary) != member.size or len(binary) > MAX_BINARY_BYTES:
                raise ValueError("Codex executable has an invalid size: " + name)
            binaries[name] = binary

    digests = {name: hashlib.sha256(binary).hexdigest() for name, binary in binaries.items()}
    for name, binary in binaries.items():
        temporary = target_files[name].with_suffix(".next")
        temporary.write_bytes(binary)
        temporary.chmod(0o755)
        temporary.replace(target_files[name])
    stamp_next = stamp.with_suffix(".next")
    stamp_next.write_text(json.dumps({"release": release, "files": digests}, sort_keys=True) + "\n",
                          encoding="utf-8")
    stamp_next.replace(stamp)
    digest_next = output_dir / "codex.sha256.next"
    digest_next.write_text(digests["codex"] + "\n", encoding="ascii")
    digest_next.replace(output_dir / "codex.sha256")
    return {"arch": arch, "version": release["version"], "sha256": digests["codex"], "files": digests}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-file", type=Path, default=Path(__file__).with_name("codex-release.json"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--archive-dir", type=Path)
    parser.add_argument("--arch", choices=sorted(TARGETS),
                        default={"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine()))
    args = parser.parse_args()
    if args.arch is None:
        parser.error("Unsupported host architecture")
    result = install(args.release_file, args.output_dir, args.arch, args.archive_dir)
    print("HABAB_CODEX_SHA256=" + result["sha256"])
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
