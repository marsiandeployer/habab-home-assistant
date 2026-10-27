#!/usr/bin/env python3
"""Install one pinned Habab Home ARM64 release into native Linux paths."""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import pwd
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import time


MIN_PYTHON = (3, 14, 2)
MAX_ARCHIVE = 220_000_000


def check_host(os_release=None, machine=None, version=None):
    """Return a clear failure for platforms outside the beta support target."""
    if os_release is None:
        data = {}
        for line in Path("/etc/os-release").read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep:
                data[key] = value.strip('"')
        os_release = data
    if os_release.get("ID") != "ubuntu" or os_release.get("VERSION_ID") != "26.04":
        raise ValueError("Supported OS: Ubuntu 26.04 LTS ARM64 only")
    machine = machine or platform.machine()
    if machine not in ("aarch64", "arm64"):
        raise ValueError("Supported architecture: ARM64 only")
    version = version or sys.version_info[:3]
    if tuple(version) < MIN_PYTHON:
        raise ValueError("Home Assistant Core 2026.10.2 requires Python 3.14.2 or newer")


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(url, maximum=MAX_ARCHIVE):
    with urllib.request.urlopen(url, timeout=90) as response:
        chunks = []
        total = 0
        while chunk := response.read(min(1024 * 1024, maximum + 1 - total)):
            total += len(chunk)
            if total > maximum:
                raise ValueError("Pinned runtime archive exceeds size limit")
            chunks.append(chunk)
        return b"".join(chunks)


def extract_opencode(archive_bytes, expected_integrity, destination):
    prefix = "sha512-"
    if not expected_integrity.startswith(prefix):
        raise ValueError("Invalid OpenCode integrity pin")
    digest = base64.b64encode(hashlib.sha512(archive_bytes).digest()).decode("ascii")
    if digest != expected_integrity[len(prefix):]:
        raise ValueError("OpenCode archive integrity check failed")
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        member = archive.getmember("package/bin/opencode")
        if not member.isfile() or member.size > MAX_ARCHIVE:
            raise ValueError("Invalid OpenCode executable in archive")
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.extractfile(member) as source, target.open("xb") as output:
            shutil.copyfileobj(source, output)
    target.chmod(0o755)
    return sha256_file(target)


def unpack_node(archive_bytes, expected_sha256, destination):
    if hashlib.sha256(archive_bytes).hexdigest() != expected_sha256:
        raise ValueError("Node.js archive SHA-256 check failed")
    target = Path(destination)
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:xz") as archive:
        members = archive.getmembers()
        for member in members:
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("Unsafe path in Node.js archive")
        root = members[0].name.split("/", 1)[0] + "/"
        selected = [member for member in members if member.name.startswith(root)]
        archive.extractall(target, members=selected, filter="data")
    inner = target / root.rstrip("/")
    for item in inner.iterdir():
        item.rename(target / item.name)
    inner.rmdir()
    if not (target / "bin/node").is_file():
        raise ValueError("Node.js archive did not contain bin/node")
    return target


def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def atomic_symlink(link, target):
    link = Path(link)
    temporary = link.with_name(link.name + ".next")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, link)


def ensure_config(config_dir, template):
    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    configuration = config_dir / "configuration.yaml"
    automations = config_dir / "automations.yaml"
    if not configuration.exists() and not configuration.is_symlink():
        if not automations.exists() and not automations.is_symlink():
            automations.write_text("[]\n")
        shutil.copy2(template, configuration)
    return configuration


def install_service(prefix, data_dir, config_dir, runtime_hash):
    etc = prefix / "etc/habab-home"
    etc.mkdir(parents=True, exist_ok=True)
    service = """[Unit]
Description=Habab Home (Home Assistant Core)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=habab
Group=habab
SupplementaryGroups=dialout
WorkingDirectory=/var/lib/habab-home/config
Environment=HOME=/var/lib/habab-home
Environment=PATH=/opt/habab-home/current/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin
Environment=HABAB_OPENCODE_BINARY=/opt/habab-home/current/bin/opencode
EnvironmentFile=/opt/habab-home/current/runtime.env
BindPaths=/var/lib/habab-home/config:/config
ExecStart=/opt/habab-home/current/venv/bin/python -m homeassistant --config /var/lib/habab-home/config
Restart=on-failure
RestartSec=10
UMask=0027
PrivateTmp=true
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
"""
    service_dir = prefix / "etc/systemd/system"
    service_dir.mkdir(parents=True, exist_ok=True)
    (service_dir / "habab-home.service").write_text(service)


def wait_for_homeassistant(probe=None, timeout=180):
    """Wait until the new Core process serves its local onboarding page."""
    probe = probe or (lambda: urllib.request.urlopen("http://127.0.0.1:8123/", timeout=2).status < 500)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if probe():
                return
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError("Home Assistant did not become ready at http://127.0.0.1:8123/")


def retain_two_releases(releases, current, previous):
    """Remove completed application releases after a successful activation."""
    keep = {link.resolve() for link in (current, previous) if link.is_symlink()}
    for path in releases.iterdir():
        if path.is_symlink() or not path.is_dir() or path.resolve() in keep:
            continue
        metadata = path / "source.json"
        if not metadata.is_file():
            continue
        release = json.loads(metadata.read_text())
        expected = f"{release['product_version']}-{release['source_sha'][:12]}"
        if path.name == expected:
            shutil.rmtree(path)


def install_release(payload, prefix=Path("/"), run_command=run, fetch_bytes=fetch,
                    perform_system_changes=True):
    payload = Path(payload)
    app = payload / "app"
    prefix = Path(prefix)
    manifest = json.loads((payload / "release.json").read_text())
    if manifest.get("architecture") != "arm64":
        raise ValueError("Package is not an ARM64 release")
    runtime_pins = json.loads((payload / "linux/runtime-pins.json").read_text())
    node_pin, opencode_pin = runtime_pins["node"], runtime_pins["opencode"]
    if node_pin.get("version") != "24.22.1" or opencode_pin.get("version") != "1.14.21":
        raise ValueError("Unexpected runtime versions in package")
    slug = f"{manifest['product_version']}-{manifest['source_sha'][:12]}"
    opt = prefix / "opt/habab-home"
    releases = opt / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    destination = releases / slug
    if destination.exists():
        raise ValueError(f"Release is already installed: {slug}")
    stage = Path(tempfile.mkdtemp(prefix=f".{slug}.", dir=releases))
    try:
        if perform_system_changes:
            run_command(["apt-get", "update"])
            run_command(["apt-get", "install", "-y", "python3.14-venv", "ca-certificates", "xz-utils"])
        py = shutil.which("python3.14") or sys.executable
        py_version = tuple(map(int, run_command(
            [py, "-c", "import sys; print('%s.%s.%s' % sys.version_info[:3])"],
            capture_output=True, text=True).stdout.strip().split(".")))
        if py_version < MIN_PYTHON:
            raise ValueError("Install Python 3.14.2 or newer before installing Habab Home")
        run_command([py, "-m", "venv", str(stage / "venv")])
        venv_py = stage / "venv/bin/python"
        requirements_lock = app / "requirements.lock"
        if requirements_lock.is_file():
            run_command([str(venv_py), "-m", "pip", "install", "--disable-pip-version-check",
                         "--require-hashes", "-r", str(requirements_lock)])
        else:
            run_command([str(venv_py), "-m", "pip", "install", "--disable-pip-version-check",
                         f"homeassistant=={manifest['home_assistant']}"])
        wheel = app / "frontend" / json.loads((app / "frontend-release.json").read_text())["filename"]
        frontend_meta = json.loads((app / "frontend-release.json").read_text())
        if wheel.stat().st_size != frontend_meta["bytes"] or sha256_file(wheel) != frontend_meta["sha256"]:
            raise ValueError("Frontend wheel failed its SHA-256 check")
        run_command([str(venv_py), "-m", "pip", "install", "--no-deps", "--no-index", str(wheel)])
        site_packages = Path(run_command(
            [str(venv_py), "-c", "import site; print(site.getsitepackages()[0])"],
            capture_output=True, text=True).stdout.strip())
        components = site_packages / "homeassistant/components"
        for name in ("habab_home", "habab_telegram", "habab_yandex_speech", "idigital_sms"):
            source = app / "src" / name
            if not source.is_dir():
                raise ValueError(f"Missing Habab integration: {name}")
            shutil.copytree(source, components / name)
        node_dir = stage / "node"
        unpack_node(fetch_bytes(node_pin["url"], 40_000_000), node_pin["sha256"], node_dir)
        executable = stage / "bin/opencode"
        open_code_hash = extract_opencode(fetch_bytes(opencode_pin["url"], MAX_ARCHIVE),
                                           opencode_pin["integrity"], executable)
        (stage / "runtime.env").write_text(f"HABAB_OPENCODE_SHA256={open_code_hash}\n")
        with (stage / "runtime.env").open("a") as env:
            env.write(f"HABAB_SOURCE_REVISION={manifest['source_sha']}\nHABAB_SOURCE_DIRTY=false\n")
        (stage / "runtime.env").chmod(0o644)
        (stage / "source.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        for tool in ("install.py", "manage.py", "update.py", "runtime-pins.json"):
            shutil.copy2(payload / "linux" / tool, stage / tool)
        for tool in ("install.py", "manage.py", "update.py"):
            (stage / tool).chmod(0o755)
        telegram_dir = app / "telegram-agent"
        shutil.copytree(telegram_dir, stage / "telegram-agent")
        (stage / "telegram-agent/node_modules").mkdir()
        (stage / "telegram-agent/node_modules/noxon-telegram").symlink_to("../vendor/noxon-telegram")
        app_root = prefix / "var/lib/habab-home"
        config_dir = app_root / "config"
        ensure_config(config_dir, app / "config/configuration.yaml")
        if perform_system_changes:
            try:
                pwd.getpwnam("habab")
            except KeyError:
                run_command(["useradd", "--system", "--home-dir", str(app_root),
                             "--shell", "/usr/sbin/nologin", "--groups", "dialout", "habab"])
            run_command(["chown", "-R", "habab:habab", str(app_root)])
            run_command(["chown", "-R", "habab:habab", str(stage)])
            run_command(["chmod", "0750", str(config_dir)])
        install_service(prefix, app_root, config_dir, open_code_hash)
        if destination.exists():
            shutil.rmtree(destination)
        stage.rename(destination)
        current = opt / "current"
        previous = opt / "previous"
        old_current = current.resolve() if current.is_symlink() else None
        old_previous = previous.resolve() if previous.is_symlink() else None
        if old_current and old_current.exists():
            atomic_symlink(previous, old_current)
        atomic_symlink(current, destination)
        telegram_link = prefix / "opt/habab-telegram"
        telegram_link.parent.mkdir(parents=True, exist_ok=True)
        if telegram_link.is_symlink() or telegram_link.exists():
            telegram_link.unlink()
        telegram_link.symlink_to(current / "telegram-agent")
        xiaomi_tool = prefix / "usr/local/bin/habab-install-xiaomi-home"
        xiaomi_tool.parent.mkdir(parents=True, exist_ok=True)
        if xiaomi_tool.is_symlink() or xiaomi_tool.exists():
            xiaomi_tool.unlink()
        shutil.copy2(app / "install-xiaomi-home.py", xiaomi_tool)
        xiaomi_tool.chmod(0o755)
        shutil.copy2(app / "xiaomi-home-release.json", xiaomi_tool.with_name("xiaomi-home-release.json"))
        habab_ha = prefix / "usr/local/bin/habab-ha"
        shutil.copy2(app / "habab-ha.py", habab_ha)
        habab_ha.chmod(0o755)
        command_dir = prefix / "usr/local/bin"
        command_dir.mkdir(parents=True, exist_ok=True)
        command = command_dir / "habab-home"
        command.unlink(missing_ok=True)
        command.symlink_to(current / "manage.py")
        if perform_system_changes:
            run_command(["systemctl", "daemon-reload"])
            run_command(["systemctl", "enable", "habab-home.service"])
            try:
                run_command(["systemctl", "restart", "habab-home.service"])
                wait_for_homeassistant()
            except Exception:
                if old_current and old_current.exists():
                    atomic_symlink(current, old_current)
                    if old_previous and old_previous.exists():
                        atomic_symlink(previous, old_previous)
                    else:
                        previous.unlink(missing_ok=True)
                    run_command(["systemctl", "restart", "habab-home.service"])
                else:
                    current.unlink(missing_ok=True)
                raise
        retain_two_releases(releases, current, previous)
        return {"release": slug, "source_sha": manifest["source_sha"],
                "frontend": manifest["frontend"], "opencode": opencode_pin["version"],
                "node": node_pin["version"], "config": str(config_dir)}
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def rollback(prefix=Path("/"), run_command=run, perform_system_changes=True):
    opt = Path(prefix) / "opt/habab-home"
    current, previous = opt / "current", opt / "previous"
    if not previous.is_symlink() or not previous.resolve().is_dir():
        raise ValueError("No previous release is available for rollback")
    old_current = current.resolve() if current.is_symlink() else None
    target = previous.resolve()
    atomic_symlink(current, target)
    if old_current and old_current.exists():
        atomic_symlink(previous, old_current)
    if perform_system_changes:
        try:
            run_command(["systemctl", "restart", "habab-home.service"])
            wait_for_homeassistant()
        except Exception:
            if old_current and old_current.exists():
                atomic_symlink(current, old_current)
                atomic_symlink(previous, target)
                run_command(["systemctl", "restart", "habab-home.service"])
            raise
    return target.name


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("payload", type=Path)
    parser.add_argument("--prefix", type=Path, default=Path("/"), help=argparse.SUPPRESS)
    parser.add_argument("--skip-system-changes", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.geteuid() != 0 and not args.skip_system_changes:
        parser.error("Run with sudo from an SSH session")
    check_host()
    print(json.dumps(install_release(args.payload, args.prefix,
                                     perform_system_changes=not args.skip_system_changes), sort_keys=True))


if __name__ == "__main__":
    main()
