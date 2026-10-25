#!/usr/bin/env python3
"""Build a clean, signed ARM64 runtime from pinned upstream + Habab sources.

Never export a running household container: its accounts/tokens must not enter
an installation package. Only a new, never-started container is exported.
"""
import argparse
import base64
import fcntl
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT.parent / 'distribution'
IMAGE = 'ghcr.io/home-assistant/home-assistant@sha256:1edf9ce6c140826cdfcd6358802cf3466e38045cd7ed62ccf42c78980ff937ae'


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def file_members(archive):
    """Reject traversal and devices; GNU tar also refuses symlink traversal."""
    for member in archive:
        name = member.name
        if name.startswith('/') or '..' in Path(name).parts:
            raise ValueError('Unsafe image path: ' + name)
        if member.isdev() or member.isfifo() or name.split('/')[0] in ('dev', 'proc', 'sys'):
            continue
        yield member


def export_image(path):
    if path.exists():
        return
    subprocess.run(['docker', 'pull', '--platform', 'linux/arm64', IMAGE], check=True)
    container = subprocess.check_output(['docker', 'create', '--platform', 'linux/arm64', '--entrypoint', '/bin/true', IMAGE], text=True).strip()
    temporary = path.with_suffix('.partial')
    try:
        with temporary.open('wb') as output:
            subprocess.run(['docker', 'export', container], stdout=output, check=True)
        temporary.rename(path)
    finally:
        subprocess.run(['docker', 'rm', container], check=True, stdout=subprocess.DEVNULL)
        temporary.unlink(missing_ok=True)


def copy_sources(rootfs):
    inventory = {}
    sources = []
    for name in ('habab_home', 'habab_telegram'):
        sources.append((DIST / 'src' / name, rootfs / 'usr/src/homeassistant/homeassistant/components' / name))
    sources.append((DIST / 'telegram-agent', rootfs / 'opt/habab-telegram'))
    allowed = {'.py', '.js', '.mjs', '.json', '.svg', '.md', '.png', '.webp', '.jpg', '.css'}
    for directory, destination in sources:
        if not directory.exists():
            continue
        if destination.exists():
            shutil.rmtree(destination)
        for source in sorted(directory.rglob('*')):
            if not source.is_file() or source.suffix not in allowed:
                continue
            if {'__pycache__', 'node_modules', 'tests', 'test', '.git'}.intersection(source.relative_to(directory).parts):
                continue
            data = source.read_bytes()
            target = destination / source.relative_to(directory)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            inventory[str(source.relative_to(DIST))] = hashlib.sha256(data).hexdigest()
    for name, target in [
        ('habab-ha.py', 'usr/local/bin/habab-ha'),
        ('install-xiaomi-home.py', 'usr/local/bin/habab-install-xiaomi-home'),
        ('xiaomi-home-release.json', 'usr/local/bin/xiaomi-home-release.json'),
    ]:
        data = (DIST / name).read_bytes()
        output = rootfs / target
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
        output.chmod(0o755 if name.endswith('.py') else 0o644)
        inventory[name] = hashlib.sha256(data).hexdigest()
    return inventory


def install_opencode(rootfs, cache):
    record = json.loads((DIST / 'opencode-release.json').read_text())['arm64']
    archive = cache / 'opencode-arm64.tgz'
    if not archive.exists():
        urllib.request.urlretrieve(record['url'], archive)
    digest = hashlib.sha512(archive.read_bytes()).digest()
    if 'sha512-' + base64.b64encode(digest).decode() != record['integrity']:
        raise ValueError('OpenCode package integrity mismatch')
    destination = rootfs / 'opt/habab-opencode'
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as bundle:
        member = bundle.getmember('package/bin/opencode')
        with bundle.extractfile(member) as source, (destination / 'opencode').open('wb') as output:
            shutil.copyfileobj(source, output)
    (destination / 'opencode').chmod(0o755)
    return record['version'], sha256(destination / 'opencode')


def install_node(rootfs, cache):
    """Fetch foreign Alpine packages on an ephemeral amd64 helper, never run ARM."""
    packages = cache / 'node-apks'
    packages.mkdir(exist_ok=True)
    # The helper uses the matching, pinned image index's amd64 member.
    amd64 = 'ghcr.io/home-assistant/home-assistant:2026.10.2@sha256:33afb0659d786213264c2211f8d91c5fe87461c56eba6a3a546eb05fc71e89f4'
    if not list(packages.glob('nodejs-*.apk')):
        subprocess.run(['docker', 'run', '--rm', '--platform', 'linux/amd64', '--entrypoint', '/bin/sh',
                        '-v', str(packages) + ':/out', '-v', str(rootfs / 'etc/apk/keys') + ':/etc/apk/keys:ro', amd64, '-ec',
                        'apk --arch aarch64 update\nexec apk fetch --recursive --arch aarch64 --output /out nodejs=24.18.1-r0'], check=True)
    if not list(packages.glob('nodejs-24.18.1-r0.apk')):
        raise ValueError('Pinned ARM64 Node package was not fetched')
    inventory = {}
    for package in sorted(packages.glob('*.apk')):
        inventory[package.name] = sha256(package)
        # APK v2 is concatenated gzip streams. tar --ignore-zeros reads the data
        # after the signature/control archives. Do not run package scripts.
        subprocess.run(['tar', '--warning=no-unknown-keyword', '--ignore-zeros', '-xzf', str(package),
                        '--exclude=./.*', '--exclude=.PKGINFO', '--exclude=.SIGN.*',
                        '--no-same-owner', '-C', str(rootfs)], check=True)
    modules = rootfs / 'opt/habab-telegram/node_modules'
    modules.mkdir(parents=True, exist_ok=True)
    link = modules / 'noxon-telegram'
    if not link.exists():
        link.symlink_to('../vendor/noxon-telegram')
    return inventory


def frontend_payload(rootfs, prefixes):
    inventory = {}
    for prefix in prefixes:
        directory = rootfs / prefix
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError('Unexpected native frontend directory')
        for path in sorted(directory.rglob('*')):
            if path.is_symlink():
                raise ValueError('Unexpected native frontend symlink')
            if path.is_file():
                inventory[str(path.relative_to(rootfs))] = sha256(path)
    encoded = json.dumps(inventory, sort_keys=True, separators=(',', ':')).encode()
    return len(inventory), hashlib.sha256(encoded).hexdigest()


def install_frontend(rootfs, cache):
    """Reuse the distribution's verified wheel; install pure Python/data for ARM."""
    spec = importlib.util.spec_from_file_location('native_frontend_installer', DIST / 'install-native-frontend.py')
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    release = json.loads((DIST / 'native-frontend-release.json').read_text())
    filename, expected_size = installer.validate_release(release)
    required = json.loads((rootfs / 'usr/src/homeassistant/homeassistant/components/frontend/manifest.json').read_text())['requirements']
    if 'home-assistant-frontend==' + release['upstream_frontend_version'] not in required:
        raise ValueError('Native frontend does not match the pinned Core version')
    wheel_directory = cache / 'native-frontend' / release['sha256']
    wheel_directory.mkdir(parents=True, exist_ok=True)
    wheel = wheel_directory / filename
    if not wheel.exists():
        with tempfile.TemporaryDirectory(dir=cache) as directory:
            verified = installer.download_wheel(release, directory)
            verified.replace(wheel)
    if wheel.stat().st_size != expected_size or sha256(wheel) != release['sha256']:
        raise ValueError('Cached native frontend checksum mismatch')
    sites = list((rootfs / 'usr/local/lib').glob('python*/site-packages'))
    if len(sites) != 1:
        raise ValueError('Expected one guest Python installation')
    site = sites[0]
    python_version = site.parent.name.removeprefix('python')
    for old in [site / 'hass_frontend', *site.glob('home_assistant_frontend-*.dist-info')]:
        if old.is_symlink():
            raise ValueError('Unexpected frontend installation symlink')
        if old.exists():
            shutil.rmtree(old)
    # No ARM execution or host credentials. The wheel is py3-none-any; target
    # version selection enforces its Python>=3.14 metadata against the guest.
    subprocess.run([sys.executable, '-m', 'pip', '--isolated', 'install', '--no-index',
                    '--no-deps', '--no-compile', '--only-binary=:all:', '--disable-pip-version-check',
                    '--python-version', python_version, '--target', str(site), str(wheel)],
                   check=True, timeout=180)
    prefixes = [str((site / 'hass_frontend').relative_to(rootfs)),
                str((site / ('home_assistant_frontend-' + release['version'] + '.dist-info')).relative_to(rootfs))]
    count, digest = frontend_payload(rootfs, prefixes)
    return {'version': release['version'], 'upstreamVersion': release['upstream_frontend_version'],
            'wheelSha256': release['sha256'], 'payloadPrefixes': prefixes,
            'payloadFiles': count, 'payloadSha256': digest}


def install_network_tools(rootfs, cache, helper_image):
    """Install explicitly requested, hash-pinned browser and Pyrogram API."""
    spec = importlib.util.spec_from_file_location(
        'android_network_tools', ROOT / 'tools/network_tools/package.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.install(rootfs, cache, helper_image)


def validate_source_revision(source_revision):
    """Attest a clean committed Android/distribution source tree before writes."""
    repository = ROOT.parents[1]
    try:
        top = subprocess.run(['git', '-C', str(repository), 'rev-parse', '--show-toplevel'],
                             check=True, capture_output=True, text=True, timeout=10).stdout.strip()
        head = subprocess.run(['git', '-C', str(repository), 'rev-parse', '--verify', 'HEAD'],
                              check=True, capture_output=True, text=True, timeout=10).stdout.strip()
        status = subprocess.run([
            'git', '-C', str(repository), 'status', '--porcelain=v1', '--untracked-files=all',
            '--', 'habab/android', 'habab/distribution',
        ], check=True, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ValueError('Runtime source provenance requires a readable Git checkout') from error
    if Path(top).resolve() != repository.resolve():
        raise ValueError('Runtime source provenance resolved to a different Git checkout')
    if head != source_revision:
        raise ValueError('source-revision does not match the checked-out Git HEAD')
    if status:
        raise ValueError('Runtime inputs in habab/android or habab/distribution are dirty')


def require_build_space(cache, rootfs, network_tools=False):
    minimum = ((2 if rootfs.exists() else 8) + (2 if network_tools else 0)) * 1024 ** 3
    available = shutil.disk_usage(cache).free
    if available < minimum:
        raise OSError(f'Runtime build needs {minimum // 1024 ** 3} GiB free; only {available // 1024 ** 2} MiB available')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--signing-key', type=Path, required=True, help='Private RSA signing key outside repository')
    parser.add_argument('--public-base', required=True, help='HTTPS directory serving this release')
    parser.add_argument('--source-revision', required=True,
                        help='Full 40-character committed source SHA used to build this runtime')
    parser.add_argument('--network-tools', action='store_true',
                        help='Include pinned headless Chromium and Kurigram tools (large download)')
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9a-f]{40}', args.source_revision):
        parser.error('source-revision must be a full 40-character lowercase hexadecimal commit SHA')
    if not args.public_base.startswith('https://'):
        parser.error('public-base must use HTTPS')
    try:
        validate_source_revision(args.source_revision)
    except ValueError as error:
        parser.error(str(error))
    cache = ROOT / 'build/runtime'
    cache.mkdir(parents=True, exist_ok=True)
    lock = (cache / 'package.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    rootfs = cache / 'rootfs'
    if not args.network_tools:
        network_state = rootfs / 'etc/habab-android-network-tools.json'
        network_pending = rootfs / 'etc/habab-android-network-tools.installing'
        if network_state.exists() or network_pending.exists():
            raise ValueError('This cached rootfs contains an opt-in network-tools install; rebuild its rootfs to omit it')
    base = cache / 'rootfs.tar'
    require_build_space(cache, rootfs, args.network_tools)
    if not rootfs.exists():
        export_image(base)
        # Validate names before GNU tar extracts; it protects against writing
        # through an earlier symlink. Absolute symlink targets are guest paths.
        with tarfile.open(base) as bundle:
            list(file_members(bundle))
        staging = Path(tempfile.mkdtemp(prefix='rootfs-extract-', dir=cache))
        try:
            subprocess.run(['tar', '-xf', str(base), '--no-same-owner', '--exclude=dev/*',
                            '--exclude=proc/*', '--exclude=sys/*', '-C', str(staging)], check=True)
            staging.rename(rootfs)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        base.unlink()
    inventory = copy_sources(rootfs)
    version, opencode_digest = install_opencode(rootfs, cache)
    node = install_node(rootfs, cache)
    native_frontend = install_frontend(rootfs, cache)
    network_tools = None
    if args.network_tools:
        helper_image = 'ghcr.io/home-assistant/home-assistant:2026.10.2@sha256:33afb0659d786213264c2211f8d91c5fe87461c56eba6a3a546eb05fc71e89f4'
        network_tools = install_network_tools(rootfs, cache, helper_image)
    launcher = rootfs / 'usr/local/share/habab-android/launch.py'
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_bytes((ROOT / 'runtime/launch.py').read_bytes())
    (rootfs / 'run').mkdir(exist_ok=True)
    (rootfs / 'run/habab-network.json').write_text('{}\n')
    # Discard upstream image build caches, including uv's empty credentials lock.
    # An installer always begins with a clean application home and no accounts.
    for name in ('.local', '.cache', '.config', '.ssh'):
        path = rootfs / 'root' / name
        if path.exists():
            shutil.rmtree(path)
    if (rootfs / 'config').exists() and any((rootfs / 'config').iterdir()):
        raise ValueError('The base image unexpectedly contains household configuration')
    for directory in ('dev', 'proc', 'sys', 'tmp', 'config', 'root'):
        (rootfs / directory).mkdir(exist_ok=True)
    # Build-time package metadata only; never seed tokens or a pre-owned setup.
    config = (DIST / 'config/configuration.yaml').read_text()
    config += '\nautomation: !include automations.yaml\nscript: !include scripts.yaml\nscene: !include scenes.yaml\nrecorder:\nhistory:\n'
    # Core defaults already listen on 8123. A custom HTTP YAML section starts
    # a five-minute confirmation trial on fresh Core and causes a restart.
    # Configure proxy trust through the owner's native HTTP settings later.
    seed = rootfs / 'usr/local/share/habab-home/configuration.yaml'
    seed.parent.mkdir(parents=True, exist_ok=True)
    seed.write_text(config)
    release = {'schema': 1, 'archiveFormat': 3, 'architecture': 'aarch64', 'coreVersion': '2026.10.2',
               'hababVersion': json.loads((DIST / 'src/habab_home/manifest.json').read_text())['version'],
               'sourceRevision': args.source_revision,
               'baseImage': IMAGE, 'opencodeVersion': version, 'opencodeSha256': opencode_digest,
               'androidLauncherSha256': sha256(launcher),
               'sourceFiles': inventory, 'nodePackages': node,
               'configurationSha256': sha256(seed), 'nativeFrontend': native_frontend}
    if network_tools is not None:
        release['networkTools'] = network_tools
    identity = hashlib.sha256(json.dumps(release, sort_keys=True).encode()).hexdigest()[:16]
    release['id'] = identity
    (rootfs / 'etc/habab-android-release.json').write_text(json.dumps(release, sort_keys=True, indent=2) + '\n')
    output = ROOT / 'dist'
    output.mkdir(exist_ok=True)
    archive = output / f'habab-runtime-{identity}-arm64.tgz'
    size = 0
    # A fresh tar has no Docker whiteouts or device nodes. Symlinks remain
    # symlinks; tarfile does not follow them while walking the guest filesystem.
    def normalized(member):
        member.uid = member.gid = 0
        member.uname = member.gname = ''
        member.mtime = 0
        member.pax_headers = {}
        return member
    partial = Path(str(archive) + '.partial')
    with partial.open('wb') as output_file, gzip.GzipFile(fileobj=output_file, mode='wb', filename='', mtime=0, compresslevel=3) as compressed, tarfile.open(fileobj=compressed, mode='w', dereference=False) as bundle:
        for path in sorted(rootfs.rglob('*')):
            relative = str(path.relative_to(rootfs))
            if relative.startswith(('tmp/', 'dev/', 'proc/', 'sys/')):
                continue
            if path.is_symlink():
                bundle.add(path, arcname=relative, recursive=False, filter=normalized)
            elif path.is_file():
                size += path.stat().st_size
                bundle.add(path, arcname=relative, recursive=False, filter=normalized)
            elif path.is_dir():
                bundle.add(path, arcname=relative, recursive=False, filter=normalized)
    partial.replace(archive)
    release.update({'url': args.public_base.rstrip('/') + '/' + archive.name,
                    'sha256': sha256(archive), 'bytes': archive.stat().st_size,
                    'unpackedBytes': size + 64 * 1024 * 1024})
    manifest = output / f'release-{identity}.json'
    manifest.write_text(json.dumps(release, sort_keys=True, indent=2) + '\n')
    signature = subprocess.check_output(['openssl', 'dgst', '-sha256', '-sign', str(args.signing_key), str(manifest)])
    Path(str(manifest) + '.sig').write_text(base64.b64encode(signature).decode() + '\n')
    assets = ROOT / 'app/src/main/assets/release'
    assets.mkdir(parents=True, exist_ok=True)
    subprocess.run(['openssl', 'pkey', '-in', str(args.signing_key), '-pubout', '-outform', 'DER',
                    '-out', str(assets / 'trusted-key.der')], check=True)
    (assets / 'default-url.txt').write_text(args.public_base.rstrip('/') + '/' + manifest.name + '\n')
    print(json.dumps({'manifest': str(manifest), 'archive': str(archive), 'bytes': release['bytes'],
                      'unpackedBytes': release['unpackedBytes'], 'sourceFiles': len(inventory)}))


if __name__ == '__main__':
    main()
