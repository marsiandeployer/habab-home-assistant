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
import uuid
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT.parent / 'distribution'
IMAGE = 'ghcr.io/home-assistant/home-assistant@sha256:1edf9ce6c140826cdfcd6358802cf3466e38045cd7ed62ccf42c78980ff937ae'
NODE_APK_HELPER = 'ghcr.io/home-assistant/home-assistant:2026.10.2@sha256:33afb0659d786213264c2211f8d91c5fe87461c56eba6a3a546eb05fc71e89f4'
NODE_APK_REPOSITORY = 'https://dl-cdn.alpinelinux.org/alpine/v3.24/main/aarch64'


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
    container = subprocess.check_output([
        'docker', 'create', '--platform', 'linux/arm64', '--memory', '256m',
        '--memory-swap', '256m', '--cpus', '1', '--pids-limit', '64',
        '--label', 'habab.qa.owner=android-runtime-producer', '--entrypoint', '/bin/true', IMAGE,
    ], text=True).strip()
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
    for name in ('habab_home', 'habab_telegram', 'habab_yandex_speech', 'idigital_sms'):
        sources.append((DIST / 'src' / name, rootfs / 'usr/src/homeassistant/homeassistant/components' / name))
    sources.append((DIST / 'telegram-agent', rootfs / 'opt/habab-telegram'))
    allowed = {'.py', '.js', '.mjs', '.json', '.svg', '.md', '.png', '.webp', '.jpg', '.css', '.vtt'}
    for directory, destination in sources:
        if not directory.exists():
            continue
        if destination.exists():
            shutil.rmtree(destination)
        for source in sorted(directory.rglob('*')):
            relative = source.relative_to(DIST)
            include_services = relative.as_posix() == 'src/idigital_sms/services.yaml'
            if not source.is_file() or (source.suffix not in allowed and not include_services):
                continue
            if {'__pycache__', 'node_modules', 'tests', 'test', '.git'}.intersection(source.relative_to(directory).parts):
                continue
            data = source.read_bytes()
            target = destination / source.relative_to(directory)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            inventory[relative.as_posix()] = hashlib.sha256(data).hexdigest()
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


def install_serialx_unix(rootfs):
    """Apply the reviewed AF_UNIX transport to the pinned Android serialx package."""
    payload = ROOT / 'runtime/serialx_unix'
    source_file = payload / 'serial_unix.py'
    patch_file = payload / 'platforms-init.patch'
    if not source_file.is_file() or not patch_file.is_file():
        raise ValueError('The serialx Unix transport patch payload is incomplete')
    patch_text = patch_file.read_text()
    if (patch_text.count('serialx/platforms/__init__.py') != 2
            or patch_text.count('+    serial_unix,  # noqa: F401') != 1):
        raise ValueError('The serialx platform registry patch differs from the reviewed overlay')

    site_packages = sorted(rootfs.glob('usr/local/lib/python*/site-packages'))
    matches = []
    for site in site_packages:
        metadata = site / 'serialx-1.11.0.dist-info/METADATA'
        if not metadata.is_file():
            continue
        fields = {}
        for line in metadata.read_text().splitlines():
            if ':' in line:
                key, value = line.split(':', 1)
                if key in {'Name', 'Version'}:
                    fields[key] = value.strip()
        if fields == {'Name': 'serialx', 'Version': '1.11.0'}:
            matches.append((site, metadata))
    if len(matches) != 1:
        raise ValueError('Android runtime must contain exactly one pinned serialx 1.11.0 package')

    site, metadata = matches[0]
    platform_dir = site / 'serialx/platforms'
    init_file = platform_dir / '__init__.py'
    if not init_file.is_file():
        raise ValueError('Pinned serialx platform registry is missing')
    init_text = init_file.read_text()
    import_line = '    serial_unix,  # noqa: F401\n'
    anchor = '    serial_socket,  # noqa: F401\n'
    if import_line not in init_text:
        if init_text.count(anchor) != 1:
            raise ValueError('Pinned serialx platform registry no longer matches the reviewed patch context')
        init_file.write_text(init_text.replace(anchor, anchor + import_line, 1))
    elif init_text.count(import_line) != 1:
        raise ValueError('Pinned serialx platform registry has a duplicate Unix transport import')

    unix_file = platform_dir / 'serial_unix.py'
    shutil.copyfile(source_file, unix_file)
    return {
        'packageVersion': '1.11.0',
        'pythonSitePackages': str(site.relative_to(rootfs)),
        'files': {
            str(init_file.relative_to(rootfs)): sha256(init_file),
            str(unix_file.relative_to(rootfs)): sha256(unix_file),
        },
    }


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


def link_telegram_vendor(rootfs):
    modules = rootfs / 'opt/habab-telegram/node_modules'
    modules.mkdir(parents=True, exist_ok=True)
    link = modules / 'noxon-telegram'
    if not link.exists():
        link.symlink_to('../vendor/noxon-telegram')


def install_node(rootfs, cache, helper_image, network_tools=False):
    """Install hash-pinned foreign Alpine packages through the guest APK database."""
    lock_path = ROOT / 'tools/network_tools/node-apks.lock.json'
    lock = json.loads(lock_path.read_text())
    if (lock.get('schema') != 1 or lock.get('architecture') != 'aarch64'
            or lock.get('repository') != NODE_APK_REPOSITORY
            or lock.get('nodeVersion') != '24.18.1-r0'):
        raise ValueError('Unsupported pinned Node APK lock')
    if helper_image != NODE_APK_HELPER:
        raise ValueError('Unexpected Node APK resolver image pin')
    release = (rootfs / 'etc/alpine-release').read_text().strip().split('.')
    if release[:2] != lock['alpineRelease'].split('.'):
        raise ValueError('Pinned Node APKs require Alpine ' + lock['alpineRelease'])
    packages = cache / 'node-apks'
    packages.mkdir(exist_ok=True)
    records = lock['packages']
    if not isinstance(records, dict) or not records:
        raise ValueError('Pinned Node APK package list is empty')
    unexpected = {path.name for path in packages.glob('*.apk')} - set(records)
    if unexpected:
        raise ValueError('Unexpected files in pinned Node APK cache: ' + ', '.join(sorted(unexpected)))
    for name, record in sorted(records.items()):
        if (Path(name).name != name or not name.endswith('.apk')
                or not re.fullmatch(r'[0-9a-f]{64}', record.get('sha256', ''))
                or not isinstance(record.get('bytes'), int)
                or record.get('url') != lock['repository'].rstrip('/') + '/' + urllib.parse.quote(name)):
            raise ValueError('Malformed pinned Node APK record: ' + name)
        package = packages / name
        if package.is_symlink():
            raise ValueError('Pinned Node APK cache contains a symlink: ' + name)
        if not package.exists():
            partial = package.with_suffix(package.suffix + '.partial')
            try:
                urllib.request.urlretrieve(record['url'], partial)
                if partial.stat().st_size != record['bytes'] or sha256(partial) != record['sha256']:
                    raise ValueError('Pinned Node APK checksum mismatch: ' + name)
                partial.replace(package)
            finally:
                partial.unlink(missing_ok=True)
        if package.stat().st_size != record['bytes'] or sha256(package) != record['sha256']:
            raise ValueError('Cached Node APK checksum mismatch: ' + name)

    # Chromium installs icu-data-full from its own pinned transaction. Installing
    # icu-data-en alongside it is an APK conflict, so leave that package out here.
    selected = [(name, record) for name, record in sorted(records.items())
                if not (network_tools and name.startswith('icu-data-en-'))]
    container_name = 'habab-node-apks-' + uuid.uuid4().hex
    command = ['docker', 'run', '--rm', '--name', container_name, '--network', 'none',
               '--platform', 'linux/amd64', '--entrypoint', '/bin/sh',
               '-v', str(rootfs) + ':/target', '-v', str(packages) + ':/node-apks:ro',
               helper_image, '-ec', 'exec "$@"', 'apk']
    apk = ['apk', '--root', '/target', '--arch', 'aarch64', '--no-network',
           '--repositories-file', '/dev/null', '--no-scripts', 'add']
    command.extend(apk + ['/node-apks/' + name for name, _ in selected])
    try:
        subprocess.run(command, check=True, timeout=300)
    finally:
        try:
            cleanup = subprocess.run(['docker', 'rm', '-f', container_name], check=False,
                                     capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            cleanup = subprocess.run(['docker', 'rm', '-f', container_name], check=False,
                                     capture_output=True, text=True, timeout=30)
        if cleanup.returncode and 'No such container' not in cleanup.stderr:
            raise RuntimeError('Unable to remove the Node APK helper container ' + container_name)

    db = rootfs / 'lib/apk/db/installed'
    installed = {}
    for block in db.read_text().split('\n\n'):
        fields = {line[0]: line[2:] for line in block.splitlines()
                  if len(line) > 2 and line[1] == ':'}
        if fields.get('P'):
            installed[fields['P']] = fields
    node = installed.get('nodejs', {})
    if node.get('V') != lock['nodeVersion'] or node.get('A') != lock['architecture']:
        raise ValueError('Pinned Node package was not registered by the APK transaction')
    expected_icu = 'icu-data-full' if network_tools else 'icu-data-en'
    if expected_icu not in installed:
        raise ValueError('Pinned Node APK transaction lacks ' + expected_icu)
    inventory = {name: record['sha256'] for name, record in selected}
    link_telegram_vendor(rootfs)
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


def install_frontend(rootfs, candidate_directory, source_revision):
    """Install this release's freshly compiled wheel, never the published pointer."""
    spec = importlib.util.spec_from_file_location('native_frontend_candidate', ROOT.parent / 'native-frontend/candidate.py')
    candidate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(candidate)
    manifest, wheel, provenance = candidate.load_candidate(candidate_directory, source_revision)
    release = {'version': manifest['wheel_version'], 'upstream_frontend_version': '20260930.4'}
    required = json.loads((rootfs / 'usr/src/homeassistant/homeassistant/components/frontend/manifest.json').read_text())['requirements']
    if 'home-assistant-frontend==' + release['upstream_frontend_version'] not in required:
        raise ValueError('Native frontend does not match the pinned Core version')
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
    return {**provenance, 'upstreamVersion': release['upstream_frontend_version'],
            'payloadPrefixes': prefixes,
            'payloadFiles': count, 'payloadSha256': digest}


def install_network_tools(rootfs, cache, helper_image):
    """Install explicitly requested, hash-pinned browser and Pyrogram API."""
    spec = importlib.util.spec_from_file_location(
        'android_network_tools', ROOT / 'tools/network_tools/package.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.install(rootfs, cache, helper_image)


def finalize_network_tools_package_database(rootfs, network_tools, node_packages):
    spec = importlib.util.spec_from_file_location(
        'android_network_tools', ROOT / 'tools/network_tools/package.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.finalize_package_database(rootfs, network_tools, node_packages)


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
            '--', 'habab/android', 'habab/distribution', 'habab/native-frontend',
        ], check=True, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ValueError('Runtime source provenance requires a readable Git checkout') from error
    if Path(top).resolve() != repository.resolve():
        raise ValueError('Runtime source provenance resolved to a different Git checkout')
    if head != source_revision:
        raise ValueError('source-revision does not match the checked-out Git HEAD')
    if status:
        raise ValueError('Runtime inputs in habab/android or habab/distribution are dirty')


def require_build_space(cache, rootfs, network_tools=False, factory_release=None):
    if factory_release is None:
        minimum = ((2 if rootfs.exists() else 8) + (2 if network_tools else 0)) * 1024 ** 3
    else:
        # Existing compressed input is already allocated. Reserve the extracted
        # factory tree, a new archive, and 1 GiB for wheel installation/headroom.
        minimum = factory_release['unpackedBytes'] + factory_release['bytes'] + 1024 ** 3
    available = shutil.disk_usage(cache).free
    if available < minimum:
        raise OSError(f'Runtime build needs {minimum // 1024 ** 3} GiB free; only {available // 1024 ** 2} MiB available')


def initial_configuration():
    # The shared source already owns automation and input_boolean. Android adds
    # its remaining native editor/history defaults without redefining them.
    return ((DIST / 'config/configuration.yaml').read_text()
            + '\nscript: !include scripts.yaml\nscene: !include scenes.yaml\nrecorder:\nhistory:\n')


def validate_factory_dependencies(release, network_tools):
    if (release.get('architecture') != 'aarch64' or release.get('coreVersion') != '2026.10.2'
            or release.get('baseImage') != IMAGE):
        raise ValueError('Factory runtime is incompatible with the pinned ARM64 Core base')
    opencode = json.loads((DIST / 'opencode-release.json').read_text())['arm64']
    if release.get('opencodeVersion') != opencode['version']:
        raise ValueError('Factory runtime OpenCode version differs from source')
    lock = json.loads((ROOT / 'tools/network_tools/node-apks.lock.json').read_text())
    expected_node = {name: record['sha256'] for name, record in lock['packages'].items()
                     if not (network_tools and name.startswith('icu-data-en-'))}
    if release.get('nodePackages') != expected_node:
        raise ValueError('Factory runtime Node packages differ from source')
    tools = release.get('networkTools')
    if bool(tools) != network_tools:
        raise ValueError('Factory runtime network-tools selection must be preserved explicitly')
    if tools:
        for field, name in (
            ('lockSha256', 'network-tools.lock.json'),
            ('browserLauncherSha256', 'habab-browser'),
            ('pythonLauncherSha256', 'habab-python-network'),
        ):
            if tools.get(field) != sha256(ROOT / 'tools/network_tools' / name):
                raise ValueError('Factory runtime network-tools input differs from source: ' + name)


def reusable_factory_release(manifest, network_tools):
    """Accept only a signed factory archive with these exact dependency pins."""
    release = json.loads(manifest.read_text())
    validate_factory_dependencies(release, network_tools)
    spec = importlib.util.spec_from_file_location('factory_runtime_verifier', ROOT / 'tools/verify_runtime.py')
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    verifier.verify(manifest, ROOT / 'app/src/main/assets/release/trusted-key.der')
    return release


def finalize_manifest(manifest, signing_key=None, unsigned_candidate=False,
                      assets=None, public_base=None):
    """Sign a release or explicitly leave it as an unsigned candidate.

    The unsigned path deliberately does not write the app trust anchor or
    default runtime URL. Those are release inputs and must remain unchanged.
    """
    signature_path = Path(str(manifest) + '.sig')
    if unsigned_candidate:
        if signature_path.exists():
            raise ValueError('Unsigned runtime candidate must not have a signature file')
        return
    if signing_key is None or assets is None or public_base is None:
        raise ValueError('Signed runtime requires a signing key and release asset destinations')
    signature = subprocess.check_output(
        ['openssl', 'dgst', '-sha256', '-sign', str(signing_key), str(manifest)])
    signature_path.write_text(base64.b64encode(signature).decode() + '\n')
    assets.mkdir(parents=True, exist_ok=True)
    subprocess.run(['openssl', 'pkey', '-in', str(signing_key), '-pubout', '-outform', 'DER',
                    '-out', str(assets / 'trusted-key.der')], check=True)
    (assets / 'default-url.txt').write_text(public_base.rstrip('/') + '/' + Path(manifest).name + '\n')


def main():
    global ROOT, DIST
    parser = argparse.ArgumentParser()
    signature_mode = parser.add_mutually_exclusive_group(required=True)
    signature_mode.add_argument('--signing-key', type=Path,
                                help='Private RSA signing key outside repository')
    signature_mode.add_argument('--unsigned-candidate', action='store_true',
                                help='Build an unsigned review artifact; never changes app trust assets')
    parser.add_argument('--public-base', required=True, help='HTTPS directory serving this release')
    parser.add_argument('--source-revision', required=True,
                        help='Full 40-character committed source SHA used to build this runtime')
    parser.add_argument('--frontend-candidate', type=Path, required=True,
                        help='Fresh native frontend candidate directory (wheel + manifest.json) from this exact source SHA')
    parser.add_argument('--source-root', type=Path,
                        help='Clean committed source checkout supplying runtime inputs; actual producer digest is recorded separately')
    parser.add_argument('--base-runtime-manifest', type=Path,
                        help='Reuse a local signed factory archive with compatible pinned dependencies; no Docker or downloads')
    parser.add_argument('--network-tools', action='store_true',
                        help='Include pinned headless Chromium and Kurigram tools (large download)')
    args = parser.parse_args()
    if args.source_root is not None:
        ROOT = args.source_root.resolve() / 'habab/android'
        DIST = ROOT.parent / 'distribution'
    if not re.fullmatch(r'[0-9a-f]{40}', args.source_revision):
        parser.error('source-revision must be a full 40-character lowercase hexadecimal commit SHA')
    if not args.public_base.startswith('https://'):
        parser.error('public-base must use HTTPS')
    try:
        validate_source_revision(args.source_revision)
    except ValueError as error:
        parser.error(str(error))
    # Reject a stale/missing wheel before exporting or downloading any runtime.
    spec = importlib.util.spec_from_file_location('native_frontend_candidate', ROOT.parent / 'native-frontend/candidate.py')
    candidate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(candidate)
    candidate.load_candidate(args.frontend_candidate, args.source_revision)
    cache = ROOT / 'build/runtime'
    cache.mkdir(parents=True, exist_ok=True)
    lock = (cache / 'package.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    rootfs = cache / 'rootfs'
    reused = None
    if args.base_runtime_manifest is not None:
        if rootfs.exists():
            raise ValueError('Factory reuse requires an empty owned rootfs cache')
        reused = reusable_factory_release(args.base_runtime_manifest.resolve(), args.network_tools)
    if not args.network_tools:
        network_state = rootfs / 'etc/habab-android-network-tools.json'
        network_pending = rootfs / 'etc/habab-android-network-tools.installing'
        if network_state.exists() or network_pending.exists():
            raise ValueError('This cached rootfs contains an opt-in network-tools install; rebuild its rootfs to omit it')
    base = cache / 'rootfs.tar'
    if reused is not None:
        base = args.base_runtime_manifest.resolve().parent / reused['url'].rsplit('/', 1)[1]
    require_build_space(cache, rootfs, args.network_tools, reused)
    if not rootfs.exists():
        if reused is None:
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
        if reused is None:
            base.unlink()
    inventory = copy_sources(rootfs)
    serialx_unix = install_serialx_unix(rootfs)
    if reused is None:
        version, opencode_digest = install_opencode(rootfs, cache)
    else:
        version, opencode_digest = reused['opencodeVersion'], reused['opencodeSha256']
    native_frontend = install_frontend(rootfs, args.frontend_candidate, args.source_revision)
    network_tools = None
    helper_image = NODE_APK_HELPER
    if reused is not None:
        network_tools = reused.get('networkTools')
        node = reused['nodePackages']
        link_telegram_vendor(rootfs)
    elif args.network_tools:
        network_tools = install_network_tools(rootfs, cache, helper_image)
    if reused is None:
        node = install_node(rootfs, cache, helper_image, network_tools=args.network_tools)
        if network_tools is not None:
            network_tools = finalize_network_tools_package_database(rootfs, network_tools, node)
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
    config = initial_configuration()
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
               'serialxUnixTransport': serialx_unix,
               'configurationSha256': sha256(seed), 'nativeFrontend': native_frontend}
    release['producerSha256'] = sha256(Path(__file__).resolve())
    if reused is not None:
        release['baseRuntime'] = {'id': reused['id'], 'archiveSha256': reused['sha256'],
                                  'sourceRevision': reused['sourceRevision']}
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
    assets = ROOT / 'app/src/main/assets/release'
    finalize_manifest(manifest, args.signing_key, args.unsigned_candidate, assets, args.public_base)
    print(json.dumps({'manifest': str(manifest), 'archive': str(archive), 'bytes': release['bytes'],
                      'unpackedBytes': release['unpackedBytes'], 'sourceFiles': len(inventory),
                      'signature': 'unsigned-candidate' if args.unsigned_candidate else 'signed'}))


if __name__ == '__main__':
    main()
