#!/usr/bin/env python3
"""Verify archive shape and install its payload as an atomic new release."""
import argparse
import os
from pathlib import Path
import sys
import tarfile
import tempfile

from install import check_host, install_release


def extract(archive_path, destination):
    with tarfile.open(archive_path, "r:gz") as package:
        members = package.getmembers()
        for member in members:
            name = Path(member.name)
            if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] != "payload":
                raise ValueError("Unsafe path in release package")
            if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                raise ValueError("Unsupported file type in release package")
        package.extractall(destination, members=members, filter="data")
    payload = Path(destination) / "payload"
    if not (payload / "release.json").is_file():
        raise ValueError("Release package is missing payload/release.json")
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run `sudo habab-home update <archive>`")
    check_host()
    with tempfile.TemporaryDirectory(prefix="habab-home-update-") as directory:
        payload = extract(args.archive, directory)
        print(install_release(payload))


if __name__ == "__main__":
    main()
