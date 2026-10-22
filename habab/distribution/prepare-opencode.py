#!/usr/bin/env python3
"""Find an existing native musl OpenCode executable; never install or copy it."""
import argparse
import hashlib
from pathlib import Path
import platform
import shutil
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument("--binary", type=Path, help="Existing Alpine/musl executable, not the Node wrapper")
parser.add_argument("--output", type=Path, default=Path(".env.opencode"))
parser.add_argument("--image", default="habab-home:0.1.0", help="Existing HA image for the compatibility check")
args = parser.parse_args()
binary = args.binary
if binary is None:
    wrapper = shutil.which("opencode")
    if not wrapper:
        parser.error("OpenCode not installed. Pass --binary with an existing musl executable.")
    arch = {"x86_64": "x64", "aarch64": "arm64"}.get(platform.machine())
    package = Path(wrapper).resolve().parent.parent
    for suffix in (["baseline-musl", "musl"] if arch == "x64" else ["musl"]):
        candidate = package / "node_modules" / f"opencode-linux-{arch}-{suffix}" / "bin/opencode"
        if candidate.is_file():
            binary = candidate
            break
if binary is None or not binary.is_file():
    parser.error("No native musl executable found; pass --binary explicitly.")
binary = binary.resolve()
if any(char in str(binary) for char in "\n\r'$# "):
    parser.error("Use an executable path without shell/env-file metacharacters.")
version = subprocess.check_output([
    "docker", "run", "--rm", "--network", "none", "--entrypoint", "/opt/opencode",
    "-v", f"{binary}:/opt/opencode:ro", args.image, "--version",
], text=True, timeout=30).strip()
digest = hashlib.sha256(binary.read_bytes()).hexdigest()
args.output.write_text(f"HABAB_OPENCODE_HOST_BINARY={binary}\nHABAB_OPENCODE_SHA256={digest}\n")
args.output.chmod(0o600)
print(f"OpenCode {version}; read-only executable selected. Settings written to {args.output}.")
