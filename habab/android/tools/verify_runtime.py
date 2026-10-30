#!/usr/bin/env python3
"""Independently check the signed release and its actual streamed payload."""
import argparse
import base64
import binascii
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import zipfile


def decode_signature(signature_path):
    encoded = Path(signature_path).read_bytes().strip()
    if not encoded:
        raise ValueError('Release signature is empty')
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError('Malformed release signature encoding') from error


def validate_source_revision(release, embedded_release=None, expected=None):
    outer = release.get('sourceRevision')
    inner = embedded_release.get('sourceRevision') if embedded_release is not None else None
    if expected is not None and not re.fullmatch(r'[0-9a-f]{40}', expected):
        raise ValueError('Expected source revision must be a full lowercase commit SHA')
    if expected is not None and outer != expected:
        raise ValueError('Signed runtime sourceRevision does not match the expected checkout')
    if embedded_release is None:
        if outer is not None and (not isinstance(outer, str) or not re.fullmatch(r'[0-9a-f]{40}', outer)):
            raise ValueError('Runtime manifest has no valid sourceRevision')
        return
    if outer is None and inner is None and expected is None:
        return
    if not isinstance(outer, str) or not re.fullmatch(r'[0-9a-f]{40}', outer):
        raise ValueError('Runtime manifest has no valid sourceRevision')
    if not isinstance(inner, str) or inner != outer:
        raise ValueError('Embedded runtime sourceRevision does not match the signed manifest')
    if expected is not None and inner != expected:
        raise ValueError('Embedded runtime sourceRevision does not match the expected checkout')


def validate_frontend_candidate(release, candidate_directory, source_revision):
    spec = importlib.util.spec_from_file_location(
        'native_frontend_candidate', Path(__file__).resolve().parents[2] / 'native-frontend/candidate.py')
    candidate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(candidate)
    _, _, expected = candidate.load_candidate(candidate_directory, source_revision)
    frontend = release.get('nativeFrontend')
    if not isinstance(frontend, dict) or any(frontend.get(key) != value for key, value in expected.items()):
        raise ValueError('Runtime frontend does not match this source and freshly built wheel')


def runtime_manifest_url(release):
    return release['url'].rsplit('/', 1)[0] + '/release-' + release['id'] + '.json'


def apk_provenance(release, manifest, apk_source_revision=None):
    provenance = {
        'sourceRevision': release['sourceRevision'],
        'runtimeId': release['id'],
        'runtimeManifestSha256': hashlib.sha256(Path(manifest).read_bytes()).hexdigest(),
        'runtimeManifestUrl': runtime_manifest_url(release),
        'nativeFrontend': release['nativeFrontend'],
    }
    # APK-only candidates keep the signed runtime's sourceRevision intact while
    # independently identifying the native Android source used for this APK.
    if apk_source_revision is not None:
        if not re.fullmatch(r'[0-9a-f]{40}', apk_source_revision):
            raise ValueError('APK source revision must be a full lowercase commit SHA')
        provenance['apkSourceRevision'] = apk_source_revision
    return provenance


def verify_apk_provenance(apk, release, manifest, apk_source_revision=None):
    with zipfile.ZipFile(apk) as archive:
        embedded = json.loads(archive.read('assets/release/build-provenance.json'))
        default_url = archive.read('assets/release/default-url.txt').decode().strip()
    if embedded != apk_provenance(release, manifest, apk_source_revision):
        raise ValueError('Signed APK does not embed this runtime and frontend provenance')
    if default_url != runtime_manifest_url(release):
        raise ValueError('Signed APK runtime download URL does not match its own release')


def verify_apk_version(apk, aapt, version_code, version_name):
    badging = subprocess.run([str(aapt), 'dump', 'badging', str(apk)], check=True,
                             capture_output=True, text=True).stdout
    package = re.search(r"(?m)^package: name='([^']+)' versionCode='([^']+)' versionName='([^']+)'", badging)
    if (package is None or package.groups() != ('ru.habab.home', str(version_code), version_name)):
        raise ValueError('Signed APK version does not match the explicit release inputs')
    return {'version_code': int(package[2]), 'version_name': package[3]}


def verify(manifest, public_key=None, expected_source_revision=None, require_serialx_unix=False,
           frontend_candidate=None, unsigned_candidate=False):
    release = json.loads(manifest.read_text())
    if frontend_candidate is not None:
        validate_frontend_candidate(release, frontend_candidate, expected_source_revision)
    serialx_unix = release.get('serialxUnixTransport')
    if require_serialx_unix and serialx_unix is None:
        raise ValueError('This release does not attest the required serialx Unix transport')
    serialx_metadata_path = None
    if serialx_unix is not None:
        if (not isinstance(serialx_unix, dict)
                or serialx_unix.get('packageVersion') != '1.11.0'
                or not isinstance(serialx_unix.get('pythonSitePackages'), str)
                or not isinstance(serialx_unix.get('files'), dict)):
            raise ValueError('Malformed serialx Unix transport attestation')
        site = serialx_unix['pythonSitePackages'].rstrip('/')
        if not re.fullmatch(r'usr/local/lib/python[0-9.]+/site-packages', site):
            raise ValueError('Unexpected serialx site-packages path')
        serialx_metadata_path = site + '/serialx-1.11.0.dist-info/METADATA'
    if expected_source_revision is not None:
        validate_source_revision(release, expected=expected_source_revision)
    signature_path = Path(str(manifest) + '.sig')
    if unsigned_candidate:
        if public_key is not None:
            raise ValueError('Unsigned candidate verification must not be given a signing key')
        if signature_path.exists():
            raise ValueError('Unsigned runtime candidate unexpectedly has a signature')
    else:
        if public_key is None:
            raise ValueError('A public key is required to verify a signed runtime')
        if not signature_path.is_file():
            raise ValueError('Signed runtime manifest signature is required')
        with tempfile.TemporaryDirectory() as directory:
            signature = Path(directory) / 'signature'
            signature.write_bytes(decode_signature(signature_path))
            key = Path(directory) / 'public.pem'
            subprocess.run(['openssl', 'pkey', '-pubin', '-inform', 'DER', '-in', str(public_key), '-out', str(key)], check=True, capture_output=True)
            subprocess.run(['openssl', 'dgst', '-sha256', '-verify', str(key), '-signature', str(signature), str(manifest)], check=True, capture_output=True)
    archive = manifest.parent / release['url'].rsplit('/', 1)[1]
    if archive.stat().st_size != release['bytes']:
        raise ValueError('Archive size mismatch')
    digest = hashlib.sha256()
    with archive.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    if digest.hexdigest() != release['sha256']:
        raise ValueError('Archive SHA-256 mismatch')
    required = {
        'usr/local/bin/python3', 'usr/local/bin/habab-ha', 'usr/local/bin/habab-install-xiaomi-home',
        'usr/bin/node', 'usr/src/homeassistant/homeassistant/components/habab_home/__init__.py',
        'usr/src/homeassistant/homeassistant/components/habab_home/frontend/setup-chat.js',
        'opt/habab-opencode/opencode', 'etc/habab-android-release.json',
        'usr/local/share/habab-android/launch.py',
    }
    telegram_link = 'opt/habab-telegram/node_modules/noxon-telegram'
    if 'telegram-agent/worker.mjs' in release.get('sourceFiles', {}):
        required.add(telegram_link)
    if serialx_unix is not None:
        required.add(serialx_metadata_path)
    files, symlinks, total = 0, 0, 0
    inventory = {}
    embedded_release = None
    serialx_metadata_version = None
    serialx_metadata_name = None
    with tarfile.open(archive, 'r|gz') as bundle:
        for member in bundle:
            if member.name.startswith('/') or '..' in Path(member.name).parts:
                raise ValueError('Unsafe archive path')
            if member.isdev() or member.isfifo():
                raise ValueError('Device/FIFO in release')
            if member.name.startswith(('config/', 'root/.ssh/', 'root/.config/', 'root/.local/share/')):
                raise ValueError('Private runtime data in installation package')
            if member.name == telegram_link and (
                    not member.issym() or member.linkname != '../vendor/noxon-telegram'):
                raise ValueError('Invalid Telegram vendored Node module link')
            required.discard(member.name)
            if member.issym():
                symlinks += 1
            elif member.isfile():
                files += 1
                total += member.size
                hash_file = hashlib.sha256()
                with bundle.extractfile(member) as source:
                    if member.name == 'etc/habab-android-release.json':
                        if embedded_release is not None or member.size > 1024 * 1024:
                            raise ValueError('Invalid embedded runtime manifest')
                        content = source.read(1024 * 1024 + 1)
                        if len(content) != member.size:
                            raise ValueError('Truncated embedded runtime manifest')
                        hash_file.update(content)
                        embedded_release = json.loads(content)
                    elif member.name == serialx_metadata_path:
                        content = source.read(1024 * 1024 + 1)
                        if len(content) != member.size:
                            raise ValueError('Truncated serialx package metadata')
                        hash_file.update(content)
                        for line in content.decode('utf-8').splitlines():
                            if line.startswith('Name: '):
                                serialx_metadata_name = line.split(':', 1)[1].strip()
                            if line.startswith('Version: '):
                                serialx_metadata_version = line.split(':', 1)[1].strip()
                    else:
                        for block in iter(lambda: source.read(1024 * 1024), b''):
                            hash_file.update(block)
                inventory[member.name] = hash_file.hexdigest()
    validate_source_revision(release, embedded_release, expected_source_revision)
    if serialx_unix is not None:
        if not isinstance(embedded_release, dict) or embedded_release.get('serialxUnixTransport') != serialx_unix:
            raise ValueError('Embedded serialx Unix transport attestation does not match the signed manifest')
    if required:
        raise ValueError('Missing runtime files: ' + ', '.join(sorted(required)))
    if serialx_unix is not None:
        if serialx_metadata_name != 'serialx' or serialx_metadata_version != serialx_unix['packageVersion']:
            raise ValueError('Pinned serialx package version mismatch')
        expected_serialx_files = serialx_unix['files']
        expected_paths = {
            serialx_unix['pythonSitePackages'] + '/serialx/platforms/__init__.py',
            serialx_unix['pythonSitePackages'] + '/serialx/platforms/serial_unix.py',
        }
        if set(expected_serialx_files) != expected_paths:
            raise ValueError('Serialx Unix transport file attestation has unexpected paths')
        for path in expected_paths:
            digest = expected_serialx_files[path]
            if (not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
                    or inventory.get(path) != digest):
                raise ValueError('Serialx Unix transport payload mismatch: ' + path)
    if total > release['unpackedBytes']:
        raise ValueError('Archive exceeds declared unpacked size')
    if inventory['opt/habab-opencode/opencode'] != release['opencodeSha256']:
        raise ValueError('OpenCode digest mismatch')
    if inventory['usr/local/share/habab-android/launch.py'] != release['androidLauncherSha256']:
        raise ValueError('Android launcher digest mismatch')
    if 'configurationSha256' in release and inventory.get('usr/local/share/habab-home/configuration.yaml') != release['configurationSha256']:
        raise ValueError('Initial configuration digest mismatch')
    if frontend := release.get('nativeFrontend'):
        if not isinstance(embedded_release, dict) or embedded_release.get('nativeFrontend') != frontend:
            raise ValueError('Embedded native frontend does not match the signed manifest')
        prefixes = tuple(prefix.rstrip('/') + '/' for prefix in frontend['payloadPrefixes'])
        frontend_files = {name: digest for name, digest in inventory.items() if name.startswith(prefixes)}
        digest = hashlib.sha256(json.dumps(frontend_files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if len(frontend_files) != frontend['payloadFiles'] or digest != frontend['payloadSha256']:
            raise ValueError('Installed native frontend payload mismatch')
    for relative, expected in release['sourceFiles'].items():
        if relative.startswith('src/'):
            path = 'usr/src/homeassistant/homeassistant/components/' + relative[4:]
        elif relative.startswith('telegram-agent/'):
            path = 'opt/habab-telegram/' + relative[len('telegram-agent/'):]
        else:
            path = {'habab-ha.py': 'usr/local/bin/habab-ha',
                    'install-xiaomi-home.py': 'usr/local/bin/habab-install-xiaomi-home',
                    'xiaomi-home-release.json': 'usr/local/bin/xiaomi-home-release.json'}[relative]
        if inventory.get(path) != expected:
            raise ValueError('Habab source snapshot mismatch: ' + relative)
    print(json.dumps({'result': 'PASS', 'signature': 'unsigned-candidate' if unsigned_candidate else 'verified',
                      'release': release['id'], 'files': files,
                      'symlinks': symlinks, 'regularBytes': total,
                      'hababSourceFiles': len(release['sourceFiles'])}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('manifest', type=Path)
    signature_mode = parser.add_mutually_exclusive_group(required=True)
    signature_mode.add_argument('--public-key', type=Path,
                                help='Trusted DER public key required for signed runtimes')
    signature_mode.add_argument('--unsigned-candidate', action='store_true',
                                help='Verify archive/provenance only; never treats it as a trusted release')
    parser.add_argument('--source-revision', help='Expected full frozen source commit SHA')
    parser.add_argument('--frontend-candidate', type=Path,
                        help='Require the exact wheel and producer identity from this fresh candidate')
    parser.add_argument('--require-serialx-unix', action='store_true',
                        help='Require the Android USB bridge unix:// transport and its signed file hashes')
    args = parser.parse_args()
    verify(args.manifest, args.public_key, args.source_revision, args.require_serialx_unix,
           args.frontend_candidate, args.unsigned_candidate)
