#!/usr/bin/env python3
"""Resolve image source labels without inventing a revision for source exports."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

_REVISION = re.compile(r"^[0-9a-f]{40,64}$")


def _git_output(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def resolve_source_provenance(root: Path, release: dict | None = None,
                              environ: dict[str, str] | None = None) -> tuple[str, str]:
    """Return (revision, dirty) from this checkout or explicit embedded metadata."""
    root = root.resolve()
    env = os.environ if environ is None else environ

    # Only trust Git metadata when this distribution context is tracked by that
    # repository. This prevents stamping an unrelated parent checkout's HEAD.
    tracked = _git_output(root, "ls-files", "--error-unmatch", "Dockerfile", "release.json")
    if tracked:
        revision = _git_output(root, "rev-parse", "--verify", "HEAD")
        if revision and _REVISION.fullmatch(revision):
            status = _git_output(root, "status", "--porcelain", "--untracked-files=all")
            dirty = "unknown" if status is None else ("true" if status else "false")
            return revision, dirty

    metadata = release if isinstance(release, dict) else {}
    revision = metadata.get("source_revision")
    dirty = metadata.get("source_dirty")
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        revision = env.get("HABAB_SOURCE_REVISION", "unknown")
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        revision = "unknown"
    if isinstance(dirty, bool):
        dirty = "true" if dirty else "false"
    elif dirty not in ("true", "false", "unknown"):
        dirty = env.get("HABAB_SOURCE_DIRTY", "unknown")
    if dirty not in ("true", "false", "unknown"):
        dirty = "unknown"
    return revision, dirty


def release_with_source(release: dict, revision: str, dirty: str) -> dict:
    """Return embedded release metadata with resolved source provenance."""
    source = dict(release)
    source["source_revision"] = revision if _REVISION.fullmatch(revision) else "unknown"
    source["source_dirty"] = dirty if dirty in ("true", "false", "unknown") else "unknown"
    return source


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--release-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        release = json.loads(args.release_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        release = {}
    revision, dirty = resolve_source_provenance(args.root, release)
    print(f"{revision} {dirty}")


if __name__ == "__main__":
    main()
