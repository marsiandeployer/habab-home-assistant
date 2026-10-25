#!/usr/bin/env python3
"""Independently check the signed release and its actual streamed payload."""
import argparse
import base64
import binascii
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile


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


def verify(manifest, public_key, expected_source_revision=None):
    release = json.loads(manifest.read_text())
    if expected_source_revision is not None:
        validate_source_revision(release, expected=expected_source_revision)
    with tempfile.TemporaryDirectory() as directory:
        signature = Path(directory) / 'signature'
        signature.write_bytes(decode_signature(Path(str(manifest) + '.sig')))
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
    files, symlinks, total = 0, 0, 0
    inventory = {}
    embedded_release = None
    with tarfile.open(archive, 'r|gz') as bundle:
        for member in bundle:
            if member.name.startswith('/') or '..' in Path(member.name).parts:
                raise ValueError('Unsafe archive path')
            if member.isdev() or member.isfifo():
                raise ValueError('Device/FIFO in release')
            if member.name.startswith(('config/', 'root/.ssh/', 'root/.config/', 'root/.local/share/')):
                raise ValueError('Private runtime data in installation package')
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
                    else:
                        for block in iter(lambda: source.read(1024 * 1024), b''):
                            hash_file.update(block)
                inventory[member.name] = hash_file.hexdigest()
    validate_source_revision(release, embedded_release, expected_source_revision)
    if required:
        raise ValueError('Missing runtime files: ' + ', '.join(sorted(required)))
    if total > release['unpackedBytes']:
        raise ValueError('Archive exceeds declared unpacked size')
    if inventory['opt/habab-opencode/opencode'] != release['opencodeSha256']:
        raise ValueError('OpenCode digest mismatch')
    if inventory['usr/local/share/habab-android/launch.py'] != release['androidLauncherSha256']:
        raise ValueError('Android launcher digest mismatch')
    if 'configurationSha256' in release and inventory.get('usr/local/share/habab-home/configuration.yaml') != release['configurationSha256']:
        raise ValueError('Initial configuration digest mismatch')
    if frontend := release.get('nativeFrontend'):
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
    print(json.dumps({'result': 'PASS', 'release': release['id'], 'files': files,
                      'symlinks': symlinks, 'regularBytes': total,
                      'hababSourceFiles': len(release['sourceFiles'])}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--public-key', type=Path, required=True)
    parser.add_argument('--source-revision', help='Expected full frozen source commit SHA')
    args = parser.parse_args()
    verify(args.manifest, args.public_key, args.source_revision)
