#!/usr/bin/env python3
"""Validate reuse of a signed Android runtime for a native-only APK candidate."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit


APK_PATHS = (
    'habab/android/app/src/main/AndroidManifest.xml',
)
APK_PREFIXES = (
    'habab/android/app/src/main/java/',
    'habab/android/app/src/main/kotlin/',
    'habab/android/app/src/main/res/',
)
EXACT_SAFE_PATHS = {
    'habab/android/tools/prepare_voice.py',
    'habab/android/tools/prepare_native.py',
    'habab/distribution/.gitignore',
    'habab/android/tools/prepare_apk_only.py',
    'habab/android/tools/test_prepare_apk_only.py',
    'habab/android/tools/verify_runtime.py',
    'habab/scripts/test_release_candidate.py',
    'habab/distribution/README-WINDOWS.md',
    'habab/distribution/Dockerfile',
    'habab/distribution/start-windows.ps1',
    'habab/distribution/package-windows.py',
    'habab/distribution/windows_package_files.py',
    'habab/distribution/build.sh',
    'habab/distribution/tests/run_release_candidate_backend_tests.py',
    'habab/distribution/tests/test_windows_package_files.py',
    'habab/distribution/tests/test_habab_product_tunnel.py',
    'habab/distribution/tests/test_habab_product_tunnel_ha.py',
}


def full_sha(value, label):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{40}', value):
        raise ValueError(f'{label} must be a full lowercase commit SHA')
    return value


def is_safe_path(path):
    if path in APK_PATHS or path in EXACT_SAFE_PATHS or path.startswith(APK_PREFIXES):
        return True
    if path.startswith('.github/workflows/'):
        return True
    if path == 'AGENTS.md' or path == 'habab/AGENTS.md':
        return True
    if path.startswith(('docs/', 'habab/docs/', 'habab/product-tunnel/')):
        return True
    if path.startswith('habab/distribution/src/habab_product_tunnel/'):
        return True
    return path == 'README.md' or path.endswith('/README.md')


def changed_paths(repo, runtime_revision, apk_revision):
    result = subprocess.run(
        ['git', '-C', str(repo), 'diff', '--no-renames', '--name-only', '-z',
         f'{runtime_revision}..{apk_revision}'],
        check=True, capture_output=True,
    ).stdout
    return [p.decode() for p in result.split(b'\0') if p]


def verify_dockerfile_reuse(repo, runtime_revision, apk_revision, release):
    def from_images(revision):
        source = subprocess.run(
            ['git', '-C', str(repo), 'show', f'{revision}:habab/distribution/Dockerfile'],
            check=True, capture_output=True, text=True,
        ).stdout
        images = [line.split()[1] for line in source.splitlines()
                  if line.lstrip().upper().startswith('FROM ') and len(line.split()) >= 2]
        if not images:
            raise ValueError('Dockerfile has no FROM image pin; use a full runtime release')
        return images

    if from_images(runtime_revision) != from_images(apk_revision):
        raise ValueError('Dockerfile FROM image changed; use a full runtime release')
    import runpy
    producer_path = Path(repo) / 'habab/android/tools/package_runtime.py'
    producer = runpy.run_path(str(producer_path))
    producer_hash = hashlib.sha256(producer_path.read_bytes()).hexdigest()
    if release.get('producerSha256') != producer_hash:
        raise ValueError('Signed runtime producer hash differs from the Android runtime packager')
    if release.get('baseImage') != producer.get('IMAGE'):
        raise ValueError('Signed runtime baseImage differs from the Android runtime packager')
    if producer.get('NODE_APK_HELPER') not in from_images(apk_revision):
        raise ValueError('Dockerfile FROM does not match the pinned Android Node APK helper image')


def verify_reuse_range(repo, runtime_revision, apk_revision, release=None):
    runtime_revision = full_sha(runtime_revision, 'Runtime source revision')
    apk_revision = full_sha(apk_revision, 'APK source revision')
    subprocess.run(['git', '-C', str(repo), 'cat-file', '-e', f'{runtime_revision}^{{commit}}'],
                   check=True, capture_output=True)
    subprocess.run(['git', '-C', str(repo), 'cat-file', '-e', f'{apk_revision}^{{commit}}'],
                   check=True, capture_output=True)
    if subprocess.run(['git', '-C', str(repo), 'merge-base', '--is-ancestor',
                       runtime_revision, apk_revision]).returncode:
        raise ValueError('Runtime source is not an ancestor of the APK source; use a full runtime release')
    paths = changed_paths(repo, runtime_revision, apk_revision)
    blocked = [path for path in paths if not is_safe_path(path)
               or (path == 'habab/distribution/Dockerfile' and release is None)]
    if blocked:
        sample = ', '.join(blocked[:8])
        more = f' (+{len(blocked) - 8} more)' if len(blocked) > 8 else ''
        raise ValueError(f'Changes can affect the runtime or APK dependencies ({sample}{more}); use a full runtime release')
    if 'habab/distribution/Dockerfile' in paths:
        verify_dockerfile_reuse(repo, runtime_revision, apk_revision, release)
    return paths


def verify_clean_apk_checkout(repo, apk_revision):
    current_head = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'],
                                  check=True, capture_output=True, text=True).stdout.strip()
    if current_head != apk_revision:
        raise ValueError('Current Git HEAD does not match the requested APK source revision')
    for args in (['diff', '--quiet'], ['diff', '--cached', '--quiet']):
        if subprocess.run(['git', '-C', str(repo), *args]).returncode:
            raise ValueError('Tracked source checkout is dirty; refusing APK-only candidate preparation')


def verify_signature(manifest_path, signature_path, public_key):
    try:
        signature = base64.b64decode(Path(signature_path).read_bytes().strip(), validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError('Signed runtime signature has invalid base64 encoding') from error
    if not signature:
        raise ValueError('Signed runtime signature is empty')
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        sig = Path(directory) / 'signature'
        pem = Path(directory) / 'public.pem'
        sig.write_bytes(signature)
        subprocess.run(['openssl', 'pkey', '-pubin', '-inform', 'DER', '-in', str(public_key),
                        '-out', str(pem)], check=True, capture_output=True)
        subprocess.run(['openssl', 'dgst', '-sha256', '-verify', str(pem), '-signature', str(sig),
                        str(manifest_path)], check=True, capture_output=True)


def verify_runtime_attestations(repo, release, runtime_revision):
    if release.get('sourceRevision') != runtime_revision:
        raise ValueError('Signed runtime sourceRevision differs from its APK reuse baseline')
    import runpy
    import tempfile
    runtime_tools = runpy.run_path(str(repo / 'habab/android/tools/package_runtime.py'))
    with tempfile.TemporaryDirectory() as temporary:
        expected = runtime_tools['copy_sources'](Path(temporary) / 'rootfs')
    if release.get('sourceFiles') != expected:
        raise ValueError('Signed runtime sourceFiles do not match the attested runtime source checkout')
    node_lock = json.loads((repo / 'habab/android/tools/network_tools/node-apks.lock.json').read_text())
    locked = node_lock.get('packages')
    if not isinstance(locked, dict) or not locked:
        raise ValueError('Android Node APK lock has no package inventory')
    network_tools = release.get('networkTools')
    expected_node = {}
    for name, record in locked.items():
        digest = record.get('sha256') if isinstance(record, dict) else None
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('Android Node APK lock has an invalid digest')
        if network_tools and name.startswith('icu-data-en-'):
            continue
        expected_node[name] = digest
    if release.get('nodePackages') != expected_node:
        raise ValueError('Signed runtime Node APK inventory differs from the current lock')
    launcher = repo / 'habab/android/runtime/launch.py'
    if release.get('androidLauncherSha256') != hashlib.sha256(launcher.read_bytes()).hexdigest():
        raise ValueError('Signed runtime launcher differs from the current source')
    config = runtime_tools['initial_configuration']()
    if release.get('configurationSha256') != hashlib.sha256(config.encode()).hexdigest():
        raise ValueError('Signed runtime seed configuration differs from the current source')
    if network_tools:
        lock = repo / 'habab/android/tools/network_tools/network-tools.lock.json'
        if network_tools.get('lockSha256') != hashlib.sha256(lock.read_bytes()).hexdigest():
            raise ValueError('Signed runtime network-tools lock differs from the current source')
        for field, relative in (
            ('browserLauncherSha256', 'habab/android/tools/network_tools/habab-browser'),
            ('pythonLauncherSha256', 'habab/android/tools/network_tools/habab-python-network'),
        ):
            if network_tools.get(field) != hashlib.sha256((repo / relative).read_bytes()).hexdigest():
                raise ValueError('Signed runtime network-tools launcher differs from the current source')


def prepare(repo, manifest_path, signature_path, public_key, apk_revision, output, manifest_url):
    repo = Path(repo).resolve()
    release = json.loads(Path(manifest_path).read_text())
    runtime_revision = full_sha(release.get('sourceRevision'), 'Signed runtime sourceRevision')
    apk_revision = full_sha(apk_revision, 'APK source revision')
    verify_clean_apk_checkout(repo, apk_revision)
    paths = verify_reuse_range(repo, runtime_revision, apk_revision, release)
    verify_signature(manifest_path, signature_path, public_key)
    verify_runtime_attestations(repo, release, runtime_revision)
    if (not isinstance(release.get('id'), str) or not re.fullmatch(r'[0-9a-f]{16}', release['id'])
            or not isinstance(release.get('bytes'), int) or release['bytes'] <= 0
            or not isinstance(release.get('sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', release['sha256'])):
        raise ValueError('Signed runtime manifest has invalid archive identity or digest fields')
    frontend = release.get('nativeFrontend')
    if (not isinstance(frontend, dict) or frontend.get('sourceRevision') != runtime_revision
            or not isinstance(frontend.get('payloadFiles'), int) or frontend['payloadFiles'] <= 0
            or not isinstance(frontend.get('payloadPrefixes'), list) or not frontend['payloadPrefixes']
            or any(not isinstance(frontend.get(field), str)
                   or not re.fullmatch(r'[0-9a-f]{64}', frontend[field])
                   for field in ('payloadSha256', 'producerManifestSha256', 'wheelSha256'))):
        raise ValueError('Signed runtime native frontend attestation is incomplete')
    url = urlsplit(release.get('url', ''))
    if (url.scheme != 'https' or url.hostname != 'habab-demo.wpmix.net'
            or url.username or url.password or url.query or url.fragment
            or not url.path.startswith('/downloads/android/')):
        raise ValueError('Signed runtime archive URL is not the canonical HTTPS downloads path')
    expected_manifest = url.path.rsplit('/', 1)[0] + '/release-' + release['id'] + '.json'
    if url.path.rsplit('/', 1)[1] != f'habab-runtime-{release["id"]}-arm64.tgz':
        raise ValueError('Signed runtime archive filename does not match its runtime ID')
    runtime_url = f'https://{url.hostname}{expected_manifest}'
    if manifest_url != runtime_url:
        raise ValueError('Input runtime manifest URL does not match the signed runtime identity')
    actual_manifest_url = release.get('manifestUrl')
    if actual_manifest_url is not None and actual_manifest_url != runtime_url:
        raise ValueError('Signed runtime manifest URL does not match its archive identity')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'build-provenance.json').write_text(json.dumps({
        'sourceRevision': runtime_revision,
        'apkSourceRevision': apk_revision,
        'runtimeId': release['id'],
        'runtimeManifestSha256': hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest(),
        'runtimeManifestUrl': runtime_url,
        'nativeFrontend': release['nativeFrontend'],
    }, indent=2, sort_keys=True) + '\n')
    (output / 'default-url.txt').write_text(runtime_url + '\n')
    return {'runtime_source_revision': runtime_revision, 'apk_source_revision': apk_revision,
            'runtime_id': release['id'], 'runtime_manifest_url': runtime_url,
            'checked_changes': len(paths)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', type=Path, default=Path('.'))
    parser.add_argument('--runtime-manifest', type=Path, required=True)
    parser.add_argument('--runtime-signature', type=Path, required=True)
    parser.add_argument('--trusted-public-key', type=Path, required=True)
    parser.add_argument('--apk-source-revision', required=True)
    parser.add_argument('--runtime-manifest-url', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = prepare(args.repo, args.runtime_manifest, args.runtime_signature,
                     args.trusted_public_key, args.apk_source_revision, args.output,
                     args.runtime_manifest_url)
    print(json.dumps({'result': 'PASS', **result}, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print(f'APK-only candidate refused: {error}', file=sys.stderr)
        raise SystemExit(1)
