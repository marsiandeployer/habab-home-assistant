#!/usr/bin/env python3
"""Install the architecture-specific, integrity-pinned OpenCode binary."""

import argparse
import base64
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import tarfile
import urllib.request

MAX_ARCHIVE_BYTES = 180_000_000
MAX_BINARY_BYTES = 250_000_000
SUPPORTED_ARCHES = {"amd64", "arm64"}
EXPECTED_BINARY = "package/bin/opencode"
EXPECTED_PACKAGES = {
    "amd64": "opencode-linux-x64-baseline-musl",
    "arm64": "opencode-linux-arm64-musl",
}


def install(release_file: Path, output_dir: Path, arch: str,
            opener=urllib.request.urlopen) -> dict:
    if arch not in SUPPORTED_ARCHES:
        raise ValueError("Unsupported target architecture")
    manifest = json.loads(release_file.read_text(encoding="utf-8"))
    release = manifest.get(arch)
    if not isinstance(release, dict):
        raise ValueError("Missing OpenCode release for target architecture")
    url = release.get("url", "")
    integrity = release.get("integrity", "")
    package_name = EXPECTED_PACKAGES[arch]
    version = release.get("version", "")
    expected_url = (
        f"https://registry.npmjs.org/{package_name}/-/"
        f"{package_name}-{version}.tgz"
    )
    if (release.get("package") != package_name or url != expected_url
            or not integrity.startswith("sha512-") or not version):
        raise ValueError("Invalid pinned OpenCode release metadata")

    with opener(url, timeout=90) as response:
        archive = response.read(MAX_ARCHIVE_BYTES + 1)
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise ValueError("OpenCode package exceeds the size limit")
    actual_integrity = base64.b64encode(hashlib.sha512(archive).digest()).decode("ascii")
    if actual_integrity != integrity.removeprefix("sha512-"):
        raise ValueError("OpenCode package integrity check failed")

    target = output_dir / "opencode"
    output_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as package:
        members = [member for member in package.getmembers()
                   if member.name == EXPECTED_BINARY]
        if len(members) != 1:
            raise ValueError("OpenCode package has no unique expected binary")
        member = members[0]
        normalized = PurePosixPath(member.name)
        if (not member.isfile() or normalized.is_absolute()
                or ".." in normalized.parts or member.size > MAX_BINARY_BYTES):
            raise ValueError("Invalid OpenCode executable in package")
        source = package.extractfile(member)
        if source is None:
            raise ValueError("OpenCode executable could not be read")
        binary = source.read(MAX_BINARY_BYTES + 1)
        if len(binary) != member.size or len(binary) > MAX_BINARY_BYTES:
            raise ValueError("OpenCode executable has an invalid size")

    digest = hashlib.sha256(binary).hexdigest()
    temporary = target.with_suffix(".next")
    temporary.write_bytes(binary)
    temporary.chmod(0o755)
    temporary.replace(target)
    (output_dir / "opencode.sha256").write_text(digest + "\n", encoding="ascii")
    return {"arch": arch, "version": release["version"], "sha256": digest}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-file", type=Path,
                        default=Path(__file__).with_name("opencode-release.json"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--arch", required=True)
    args = parser.parse_args()
    print(json.dumps(install(args.release_file, args.output_dir, args.arch), sort_keys=True))


if __name__ == "__main__":
    main()
