#!/usr/bin/env python3
"""Preview and manage one home's product-tunnel relay fragments in a sandbox root."""

from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path


STATE_DIR = Path("var/lib/habab-product-tunnel/homes")
SSH_DIR = Path("etc/ssh/sshd_config.d")
KEY_DIR = Path("etc/habab-product-tunnel/keys")
NGINX_DIR = Path("etc/nginx/conf.d")
HOST_SUFFIX = ".remote.habab.ru"
ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")
HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
KEY_RE = re.compile(r"^ssh-ed25519 ([A-Za-z0-9+/]+={0,2})(?: ([^\r\n]*))?$")
LEGACY_MARKER = "habab-product-tunnel-managed-v1"
MARKER = "habab-product-tunnel-managed-v2"
MANIFEST_VERSION = 2


class ManageError(Exception):
    pass


def validate_home(home_id: str, host: str, port: int, public_key: str) -> tuple[str, str, int, str]:
    if not isinstance(home_id, str) or not isinstance(host, str) or not isinstance(public_key, str):
        raise ManageError("home id, host, and public key must be text")
    if not ID_RE.fullmatch(home_id):
        raise ManageError("home id must be 1-32 lowercase letters, digits, or internal hyphens")
    normalized_host = host.rstrip(".").lower()
    expected_host = f"home-{home_id}{HOST_SUFFIX}"
    if not HOST_RE.fullmatch(normalized_host) or normalized_host != expected_host:
        raise ManageError(f"host must be exactly {expected_host}")
    if isinstance(port, bool) or not 32000 <= port <= 39999:
        raise ManageError("loopback port must be in 32000-39999")
    match = KEY_RE.fullmatch(public_key.strip())
    if not match:
        raise ManageError("public key must be one ssh-ed25519 authorized_keys line")
    try:
        blob = base64.b64decode(match.group(1), validate=True)
    except ValueError as err:
        raise ManageError("public key has invalid base64 data") from err
    expected_blob_prefix = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
    if not blob.startswith(expected_blob_prefix) or len(blob) != len(expected_blob_prefix) + 32:
        raise ManageError("public key does not contain a valid Ed25519 key blob")
    comment = match.group(2) or ""
    if any(ord(char) < 32 or ord(char) == 127 for char in comment):
        raise ManageError("public key comment must not contain control characters")
    return home_id, normalized_host, port, f"ssh-ed25519 {match.group(1)}" + (f" {comment}" if comment else "")


def public_key_blob(public_key: str) -> bytes:
    match = KEY_RE.fullmatch(public_key.strip())
    if not match:
        raise ManageError("public key must be one ssh-ed25519 authorized_keys line")
    return base64.b64decode(match.group(1), validate=True)


def render(home_id: str, host: str, port: int, public_key: str, tls_termination: str = "edge") -> dict[Path, bytes]:
    user = f"habab-home-{home_id}"
    key_line = (
        f'restrict,port-forwarding,permitlisten="127.0.0.1:{port}",'
        f'command="/usr/bin/false" {public_key}\n'
    )
    sshd = f"""# {MARKER}; generated for {home_id}
Match User {user}
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AuthorizedKeysFile /etc/habab-product-tunnel/keys/{home_id}
    AllowTcpForwarding remote
    AllowStreamLocalForwarding no
    GatewayPorts no
    PermitListen 127.0.0.1:{port}
    PermitOpen none
    PermitTunnel no
    AllowAgentForwarding no
    X11Forwarding no
    PermitTTY no
    PermitUserRC no
    MaxSessions 0
    ForceCommand /usr/bin/false
Match all
"""
    if tls_termination == "edge":
        nginx = f"""# {MARKER}; generated for {home_id}; TLS terminates at the public edge
server {{
    listen 80;
    server_name {host};
    if ($host != {host}) {{ return 444; }}

    location / {{
        proxy_pass http://127.0.0.1:{port};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 3600s;
        proxy_buffering off;
    }}
}}
"""
    elif tls_termination == "local":
        nginx = f"""# {MARKER}; generated for {home_id}; TLS terminates on this relay
server {{
    listen 443 ssl;
    server_name {host};
    if ($host != {host}) {{ return 444; }}

    ssl_certificate /etc/letsencrypt/live/{host}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{host}/privkey.pem;

    location / {{
        proxy_pass http://127.0.0.1:{port};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 3600s;
        proxy_buffering off;
    }}
}}
"""
    else:
        raise ManageError("tls termination must be edge or local")
    return {
        SSH_DIR / f"80-habab-product-{home_id}.conf": sshd.encode(),
        KEY_DIR / home_id: key_line.encode(),
        NGINX_DIR / f"habab-product-{home_id}.conf": nginx.encode(),
    }


def _root(path: str) -> Path:
    root = Path(path).expanduser()
    if not root.is_absolute():
        raise ManageError("sandbox root must be an absolute path")
    if root == Path("/"):
        raise ManageError("filesystem root cannot be used as a sandbox root")
    return root.resolve(strict=False)


def _check_no_symlinks(root: Path, relative: Path) -> None:
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ManageError(f"refusing symlink in managed path: {relative}")


def _read(root: Path, relative: Path) -> bytes | None:
    _check_no_symlinks(root, relative)
    target = root / relative
    try:
        mode = target.lstat().st_mode
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(mode):
        raise ManageError(f"refusing non-regular managed path: {relative}")
    return target.read_bytes()


def _manifest_path(home_id: str) -> Path:
    return STATE_DIR / f"{home_id}.json"


def _load_manifest(root: Path, home_id: str) -> dict | None:
    raw = _read(root, _manifest_path(home_id))
    if raw is None:
        return None
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise ManageError(f"invalid managed state for {home_id}") from err
    if (not isinstance(manifest, dict) or manifest.get("marker") not in (MARKER, LEGACY_MARKER)
            or manifest.get("home_id") != home_id or not isinstance(manifest.get("files"), dict)):
        raise ManageError(f"state does not prove ownership of {home_id}")
    if manifest["marker"] == MARKER:
        if manifest.get("manifest_version") != MANIFEST_VERSION:
            raise ManageError(f"unsupported managed state version for {home_id}")
        tls_termination = manifest.get("tls_termination")
    else:
        tls_termination = manifest.get("tls_termination", "local")
        if tls_termination != "local":
            raise ManageError(f"invalid legacy managed state for {home_id}")
    if tls_termination not in ("edge", "local"):
        raise ManageError(f"invalid TLS termination mode for {home_id}")
    try:
        _, host, port, public_key = validate_home(
            home_id, manifest["host"], manifest["port"], manifest["public_key"]
        )
    except (KeyError, TypeError) as err:
        raise ManageError(f"invalid managed state for {home_id}") from err
    expected_paths = {str(path) for path in render(home_id, host, port, public_key, tls_termination)}
    if set(manifest["files"]) != expected_paths:
        raise ManageError(f"managed state paths are invalid for {home_id}")
    for relative, digest in manifest["files"].items():
        current = _read(root, Path(relative))
        if current is None or hashlib.sha256(current).hexdigest() != digest:
            raise ManageError(f"managed file changed or missing; refusing to overwrite/remove: {relative}")
    return manifest


def _all_manifests(root: Path, skip_id: str | None = None) -> list[dict]:
    directory = root / STATE_DIR
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise ManageError("managed state directory is not a regular directory")
    manifests = []
    for item in sorted(directory.glob("*.json")):
        if item.stem == skip_id:
            continue
        if not ID_RE.fullmatch(item.stem):
            raise ManageError(f"unexpected state file name: {item.name}")
        manifest = _load_manifest(root, item.stem)
        if manifest is None:
            raise ManageError(f"missing managed state: {item.name}")
        manifests.append(manifest)
    return manifests


def _diff(path: Path, old: bytes | None, new: bytes | None) -> str:
    before = old.decode("utf-8").splitlines(keepends=True) if old is not None else []
    after = new.decode("utf-8").splitlines(keepends=True) if new is not None else []
    return "".join(difflib.unified_diff(before, after, fromfile=f"current/{path}", tofile=f"desired/{path}"))


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _apply(root: Path, files: dict[Path, bytes], manifest_path: Path, manifest: bytes | None) -> None:
    for relative, content in files.items():
        _check_no_symlinks(root, relative)
        target = root / relative
        if target.exists() and _read(root, relative) is None:
            raise ManageError(f"refusing to replace unmanaged path: {relative}")
        mode = 0o600 if relative.parent == KEY_DIR or relative == manifest_path else 0o644
        _atomic_write(target, content, mode)
    _check_no_symlinks(root, manifest_path)
    _atomic_write(root / manifest_path, manifest or b"", 0o600)


def _manifest(home_id: str, host: str, port: int, public_key: str, tls_termination: str,
              files: dict[Path, bytes]) -> bytes:
    data = {
        "marker": MARKER,
        "manifest_version": MANIFEST_VERSION,
        "home_id": home_id,
        "host": host,
        "port": port,
        "public_key": public_key,
        "tls_termination": tls_termination,
        "files": {str(path): hashlib.sha256(content).hexdigest() for path, content in files.items()},
    }
    return (json.dumps(data, sort_keys=True, indent=2) + "\n").encode()


def provision(args: argparse.Namespace) -> int:
    home_id, host, port, public_key = validate_home(args.home_id, args.host, args.port, args.public_key)
    root = _root(args.sandbox_root)
    files = render(home_id, host, port, public_key, args.tls_termination)
    manifest_path = _manifest_path(home_id)
    old_manifest = _load_manifest(root, home_id)
    for other in _all_manifests(root, skip_id=home_id):
        if other["host"] == host or other["port"] == port:
            raise ManageError(f"host or loopback port is already assigned to home {other['home_id']}")
        if public_key_blob(other["public_key"]) == public_key_blob(public_key):
            raise ManageError(f"public key is already assigned to home {other['home_id']}")
    for relative in files:
        _check_no_symlinks(root, relative)
        if _read(root, relative) is not None and old_manifest is None:
            raise ManageError(f"refusing to replace unmanaged path: {relative}")
    state = _manifest(home_id, host, port, public_key, args.tls_termination, files)
    diffs = "".join(_diff(path, _read(root, path), content) for path, content in files.items())
    diffs += _diff(manifest_path, _read(root, manifest_path), state)
    if diffs:
        sys.stdout.write(diffs)
    else:
        print("No changes.")
    if args.apply:
        _apply(root, files, manifest_path, state)
        print(f"Applied home {home_id} under sandbox root {root}.")
    else:
        print("Preview only. Re-run with --apply to write these files.")
    return 0


def revoke(args: argparse.Namespace) -> int:
    if not ID_RE.fullmatch(args.home_id):
        raise ManageError("home id must be 1-32 lowercase letters, digits, or internal hyphens")
    root = _root(args.sandbox_root)
    manifest = _load_manifest(root, args.home_id)
    if manifest is None:
        print(f"No managed home entry for {args.home_id}.")
        return 0
    files = {Path(path): None for path in manifest["files"]}
    state_path = _manifest_path(args.home_id)
    diffs = "".join(_diff(path, _read(root, path), None) for path in files)
    diffs += _diff(state_path, _read(root, state_path), None)
    sys.stdout.write(diffs)
    if args.apply:
        for relative in files:
            _check_no_symlinks(root, relative)
            (root / relative).unlink()
        _check_no_symlinks(root, state_path)
        (root / state_path).unlink()
        print(f"Revoked only home {args.home_id} under sandbox root {root}.")
    else:
        print("Preview only. Re-run with --apply to remove these files.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    add = subparsers.add_parser("provision", help="render or write one home's relay configuration")
    add.add_argument("--home-id", required=True)
    add.add_argument("--host", required=True)
    add.add_argument("--port", required=True, type=int)
    add.add_argument("--public-key", required=True)
    add.add_argument("--sandbox-root", required=True)
    add.add_argument("--tls-termination", choices=("edge", "local"), default="edge",
                     help="where HTTPS terminates (default: edge; local emits a TLS vhost)")
    add.add_argument("--apply", action="store_true", help="write files; default behavior is preview only")
    add.set_defaults(run=provision)
    remove = subparsers.add_parser("revoke", help="preview or remove one managed home's files")
    remove.add_argument("--home-id", required=True)
    remove.add_argument("--sandbox-root", required=True)
    remove.add_argument("--apply", action="store_true", help="remove files; default behavior is preview only")
    remove.set_defaults(run=revoke)
    args = parser.parse_args()
    try:
        return args.run(args)
    except (ManageError, OSError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
