#!/usr/bin/env python3
"""Install the SHA-pinned frontend built from the Core-matched upstream tag."""

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlparse
import urllib.request

MAX_WHEEL_BYTES = 150_000_000
ARTIFACT_ORIGIN = "https://habab-demo.wpmix.net"


def validate_release(release):
    """Only accept the complete wheel identified by the tracked release file."""
    digest = release.get("sha256", "")
    filename = release.get("filename", "")
    version = release.get("version", "")
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("Invalid frontend SHA-256")
    if not re.fullmatch(r"\d{8}\.\d+\+habab\.\d+", version):
        raise ValueError("Invalid frontend version")
    if filename != f"home_assistant_frontend-{version}-py3-none-any.whl":
        raise ValueError("Unexpected frontend wheel filename")
    expected = f"{ARTIFACT_ORIGIN}/downloads/native-frontend/{digest}/{filename}"
    if release.get("url") != expected:
        raise ValueError("Unexpected frontend artifact URL")
    size = release.get("bytes")
    if type(size) is not int or not 0 < size <= MAX_WHEEL_BYTES:
        raise ValueError("Invalid frontend wheel size")
    return filename, size


def download_wheel(release, directory, opener=urllib.request.urlopen):
    filename, expected_size = validate_release(release)
    target = Path(directory) / filename
    digest = hashlib.sha256()
    total = 0
    with opener(release["url"], timeout=90) as response, target.open("xb") as output:
        # A same-origin public artifact must not redirect to another service.
        final_url = urlparse(response.geturl())
        if (final_url.scheme, final_url.netloc) != ("https", "habab-demo.wpmix.net"):
            raise ValueError("Unexpected frontend artifact redirect")
        while chunk := response.read(min(1024 * 1024, expected_size + 1 - total)):
            total += len(chunk)
            if total > expected_size:
                raise ValueError("Frontend wheel exceeds its pinned size")
            digest.update(chunk)
            output.write(chunk)
    if total != expected_size or digest.hexdigest() != release["sha256"]:
        raise ValueError("Frontend wheel integrity check failed")
    return target


def install(release_file=None):
    release_file = release_file or Path(__file__).with_name("native-frontend-release.json")
    release = json.loads(Path(release_file).read_text())
    with tempfile.TemporaryDirectory(prefix="habab-native-frontend-") as directory:
        wheel = download_wheel(release, directory)
        subprocess.run([
            sys.executable, "-m", "pip", "install", "--no-deps", "--no-index",
            "--disable-pip-version-check", str(wheel),
        ], check=True)
    return {"installed": True, "version": release["version"], "sha256": release["sha256"]}


if __name__ == "__main__":
    print(json.dumps(install()))
