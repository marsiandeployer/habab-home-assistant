#!/usr/bin/env python3
"""Check official signed Alpine metadata and locked inputs before a large build."""
import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zlib

from package_runtime import NODE_APK_REPOSITORY, ROOT, node_package_url


def download_small(url, limit):
    with urllib.request.urlopen(url, timeout=15) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('Official Alpine metadata exceeds size limit')
    return data


def verify_signed_index(raw, public_key, key_name):
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    signature_tar = decoder.decompress(raw, 1024 * 1024)
    payload = decoder.unused_data
    if not decoder.eof or not payload:
        raise ValueError('Malformed signed Alpine APKINDEX')
    with tarfile.open(fileobj=io.BytesIO(signature_tar)) as archive:
        member = archive.getmember('.SIGN.RSA.' + key_name)
        if not member.isreg() or member.size > 8192:
            raise ValueError('Invalid Alpine index signature')
        signature = archive.extractfile(member).read()
    with tempfile.TemporaryDirectory(prefix='habab-apkindex-') as temporary:
        root = Path(temporary)
        (root / 'key.pem').write_bytes(public_key)
        (root / 'signature').write_bytes(signature)
        (root / 'payload.gz').write_bytes(payload)
        # Alpine v3.24 APKINDEX uses its official legacy RSA/SHA-1 signature.
        subprocess.run(['openssl', 'dgst', '-sha1', '-verify', str(root / 'key.pem'),
                        '-signature', str(root / 'signature'), str(root / 'payload.gz')],
                       check=True, capture_output=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as archive:
        member = archive.getmember('APKINDEX')
        if not member.isreg() or member.size > 16 * 1024 * 1024:
            raise ValueError('Alpine index content exceeds size limit')
        text = archive.extractfile(member).read().decode()
    entries = {}
    for block in text.split('\n\n'):
        fields = dict(line.split(':', 1) for line in block.splitlines() if ':' in line)
        if 'P' in fields:
            entries[fields['P'] + '-' + fields['V'] + '.apk'] = fields
    return entries


def validate_index_lock(lock, entries):
    selected = {}
    providers = {}
    for name, record in lock['packages'].items():
        node_package_url(name, record, NODE_APK_REPOSITORY)
        entry = entries.get(name, {})
        if (entry.get('A') != lock['architecture'] or entry.get('S') != str(record['bytes'])
                or not record.get('indexChecksum') or entry.get('C') != record['indexChecksum']):
            raise ValueError('Locked package differs from the signed official index: ' + name)
        selected[name] = entry
        providers[entry['P']] = entry['V']
        for provided in entry.get('p', '').split():
            atom, _, version = provided.partition('=')
            providers[atom] = version
    for name, entry in selected.items():
        for dependency in entry.get('D', '').split():
            atom, equal, version = dependency.partition('=')
            if atom not in providers or (equal and providers[atom] != version):
                raise ValueError(f'Incomplete locked Alpine dependency transaction: {name}: {dependency}')


def check_url(name, url, expected_bytes=None):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method='HEAD'), timeout=15) as response:
            length = response.headers.get('Content-Length')
            if response.status != 200 or (expected_bytes is not None and length is not None
                                          and int(length) != expected_bytes):
                raise ValueError('HTTP status/size differs from the locked input')
    except Exception as error:
        raise RuntimeError(f'Official locked input unavailable: {name}: {error}') from error
    return name


def check_record(item):
    name, record = item
    url = node_package_url(name, record, NODE_APK_REPOSITORY)
    return check_url(name, url, record['bytes'])


def check_native_and_voice_inputs():
    native = json.loads((ROOT / 'native-packages.json').read_text())
    for name, record in sorted(native.items()):
        filename = record['Filename']
        if filename.startswith('/') or '..' in Path(filename).parts or '?' in filename or '#' in filename:
            raise ValueError('Unsafe official Termux package path: ' + name)
        url = 'https://packages.termux.dev/apt/termux-main/' + filename
        check_url('Termux ' + name, url)
        print('Available official Termux input: ' + filename)
    voice = json.loads((ROOT / 'voice-model.json').read_text())
    expected = 'https://alphacephei.com/vosk/models/' + voice['name'] + '.zip'
    if voice['url'] != expected:
        raise ValueError('Vosk input must use its official model URL')
    check_url('Vosk ' + voice['name'], voice['url'], voice['bytes'])
    print('Available official Vosk input: ' + voice['name'])


def main():
    lock = json.loads((ROOT / 'tools/network_tools/node-apks.lock.json').read_text())
    if lock.get('repository') != NODE_APK_REPOSITORY or not lock.get('packages'):
        raise ValueError('Unexpected or empty Node APK repository lock')
    index = lock['upstreamIndex']
    if index['url'] != NODE_APK_REPOSITORY + '/APKINDEX.tar.gz':
        raise ValueError('APKINDEX must come from the official locked Alpine repository')
    key_url = urllib.parse.urlsplit(index['signingKeyUrl'])
    key_name = Path(key_url.path).name
    if (key_url.scheme != 'https' or key_url.hostname != 'alpinelinux.org' or key_url.username
            or key_url.password or key_url.query or key_url.fragment or key_url.port not in (None, 443)
            or key_url.path != '/keys/' + key_name or not key_name.endswith('.rsa.pub')):
        raise ValueError('Alpine signing key must come from alpinelinux.org/keys')
    key = download_small(index['signingKeyUrl'], 8192)
    if hashlib.sha256(key).hexdigest() != index['signingKeySha256']:
        raise ValueError('Official Alpine signing key fingerprint changed')
    raw = download_small(index['url'], 4 * 1024 * 1024)
    entries = verify_signed_index(raw, key, key_name)
    validate_index_lock(lock, entries)
    print('Verified official APKINDEX signature and complete locked dependency transaction')
    print('Current official APKINDEX SHA-256: ' + hashlib.sha256(raw).hexdigest())
    with ThreadPoolExecutor(max_workers=4) as pool:
        for name in pool.map(check_record, sorted(lock['packages'].items())):
            print('Available official pinned Node APK: ' + name)
    check_native_and_voice_inputs()
    print('Availability and signed metadata checked; producers still verify full SHA-256/bytes and package signatures.')


if __name__ == '__main__':
    main()
