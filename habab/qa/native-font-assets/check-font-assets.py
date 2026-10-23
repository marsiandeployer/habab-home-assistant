#!/usr/bin/env python3
"""Verify Roboto assets survive gather-static and Python wheel packaging."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from zipfile import BadZipFile, ZipFile

REQUIRED = (
    "Roboto-Regular.woff2",
    "Roboto-Medium.woff2",
    "Roboto-Bold.woff2",
)
FONT_PREFIX = "hass_frontend/static/fonts/roboto/"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(data: bytes) -> dict[str, object]:
    if len(data) < 4 or data[:4] != b"wOF2":
        raise ValueError("Font data does not have the WOFF2 wOF2 signature")
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "magic": "wOF2"}


def check_static(static_dir: Path) -> tuple[dict[str, dict[str, object]], int, bool]:
    font_dir = static_dir / "fonts" / "roboto"
    fonts = {}
    for filename in REQUIRED:
        file = font_dir / filename
        if not file.is_file() or file.stat().st_size == 0:
            raise ValueError(f"Missing or empty required static font: {filename}")
        fonts[filename] = fingerprint(file.read_bytes())

    font_count = sum(1 for file in font_dir.rglob("*.woff2") if file.is_file())
    if font_count < 12:
        raise ValueError(f"Expected at least 12 Roboto WOFF2 files in static output, found {font_count}")

    resolved = static_dir.resolve()
    dotted_parent = any("." in component for component in resolved.parts[:-1])
    return fonts, font_count, dotted_parent


def check_wheel(
    wheel_path: Path, static_fonts: dict[str, dict[str, object]]
) -> tuple[dict[str, dict[str, object]], int]:
    try:
        with ZipFile(wheel_path) as wheel:
            infos = {info.filename: info for info in wheel.infolist()}
            fonts = {}
            for filename in REQUIRED:
                member = FONT_PREFIX + filename
                info = infos.get(member)
                if info is None or info.file_size == 0:
                    raise ValueError(f"Missing or empty required wheel member: {member}")
                font = fingerprint(wheel.read(member))
                if font != static_fonts[filename]:
                    raise ValueError(f"Static and wheel font bytes differ for {filename}")
                fonts[filename] = font

            font_members = [
                info
                for name, info in infos.items()
                if name.startswith(FONT_PREFIX) and name.endswith(".woff2")
            ]
            if len(font_members) < 12:
                raise ValueError(
                    f"Expected at least 12 Roboto WOFF2 wheel members, found {len(font_members)}"
                )
            return fonts, len(font_members)
    except BadZipFile as error:
        raise ValueError(f"Invalid wheel archive: {wheel_path.name}") from error


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--static-dir", required=True, type=Path)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    static_fonts, static_font_count, dotted_parent = check_static(args.static_dir)
    result: dict[str, object] = {
        "static_required_fonts": static_fonts,
        "static_woff2_count": static_font_count,
        "source_tree_has_dotted_parent": dotted_parent,
    }
    if args.wheel:
        wheel_fonts, wheel_font_count = check_wheel(args.wheel, static_fonts)
        result.update(
            wheel_filename=args.wheel.name,
            wheel_sha256=sha256(args.wheel),
            wheel_bytes=args.wheel.stat().st_size,
            wheel_required_fonts=wheel_fonts,
            wheel_woff2_count=wheel_font_count,
        )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as error:
        print(f"font asset validation failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
