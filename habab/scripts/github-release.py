#!/usr/bin/env python3
"""Verify every candidate payload before staging an immutable GitHub Release."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlsplit
import zipfile

_SPEC = importlib.util.spec_from_file_location('release_candidate', Path(__file__).with_name('release-candidate.py'))
candidate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(candidate)
TARGETS = {'windows', 'linux-amd64', 'linux-arm64', 'android'}
MAX_ASSET_BYTES = 2 * 1024 ** 3 - 1


def digest(path):
    return candidate._digest(path)


def release_base(repository, tag, source_revision):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid GitHub repository')
    if not re.fullmatch(r'habab-v[0-9][0-9A-Za-z.+_-]{0,62}-[0-9a-f]{12}', tag):
        raise ValueError('Release tag must be habab-v<VERSION>-<12-character source SHA>')
    if not re.fullmatch(r'[0-9a-f]{40}', source_revision) or not tag.endswith('-' + source_revision[:12]):
        raise ValueError('Release tag does not identify this source SHA')
    return f'https://github.com/{repository}/releases/download/{tag}'


# Product builds may reuse a frozen SHA only across reviewed orchestration-only commits.
CI_ONLY_PATHS = {
    '.github/workflows/habab-release.yaml',
    '.github/workflows/habab-release-candidate.yaml',
    'habab/scripts/github-release.py',
    'habab/scripts/test_github_release.py',
    'habab/scripts/test_release_candidate.py',
    'habab/docs/RELEASE-CANDIDATE.md',
}


def validate_controller_range(repository, product_revision, workflow_revision):
    if not all(re.fullmatch(r'[0-9a-f]{40}', value) for value in (product_revision, workflow_revision)):
        raise ValueError('Product and controller revisions must be full lowercase SHAs')
    def git(*arguments):
        return subprocess.check_output(['git', '-C', str(repository), *arguments])
    try:
        git('merge-base', '--is-ancestor', product_revision, workflow_revision)
        commits = git('rev-list', product_revision + '..' + workflow_revision).decode().splitlines()
        touched = set()
        for commit in commits:
            touched.update(path.decode() for path in git('diff-tree', '--no-commit-id', '--name-only',
                                                         '--no-renames', '-r', '-m', '-z', commit).split(b'\0') if path)
    except subprocess.CalledProcessError as error:
        raise ValueError('Frozen product must be an ancestor of the controller revision') from error
    unexpected = touched - CI_ONLY_PATHS
    if unexpected:
        raise ValueError('Product changed in the complete controller range: ' + ', '.join(sorted(unexpected)))


def validate_frontend_run(run, repository, product_revision):
    if (run.get('head_sha') != product_revision or run.get('status') != 'completed'
            or run.get('event') != 'workflow_dispatch'
            or run.get('repository', {}).get('full_name') != repository
            or run.get('path') not in {'.github/workflows/habab-release.yaml',
                                       '.github/workflows/habab-native-frontend-candidate.yaml'}):
        raise ValueError('Frontend reuse requires a completed trusted workflow run at the exact product SHA')


def verify_uploaded(receipt, remote):
    """GitHub's digest is the checksum computed from actual uploaded asset bytes."""
    expected = {item['name']: item for item in receipt['assets']}
    actual = remote.get('assets', [])
    if len(actual) != len(expected) or {item.get('name') for item in actual} != set(expected):
        raise ValueError('Uploaded GitHub asset inventory differs from release receipt')
    for item in actual:
        record = expected[item['name']]
        if (item.get('state') != 'uploaded' or item.get('size') != record['bytes']
                or item.get('digest') != 'sha256:' + record['sha256']):
            raise ValueError(f"GitHub asset checksum/size mismatch: {item['name']}")


def stage(root, source_tree, source_revision, repository, tag, output, workflow_revision=None):
    base = release_base(repository, tag, source_revision)
    workflow_revision = workflow_revision or source_revision
    if not re.fullmatch(r'[0-9a-f]{40}', workflow_revision):
        raise ValueError('Expected controller workflow revision must be a full SHA')
    manifest = json.loads((root / 'release-candidate.json').read_text())
    if (manifest.get('source_revision') != source_revision or manifest.get('source_dirty') is not False
            or manifest.get('workflow_revision') != workflow_revision
            or set(manifest.get('targets', {})) != TARGETS):
        raise ValueError('Release requires all four targets at the exact clean source SHA')
    for name, status in manifest['targets'].items():
        if status.get('state') != 'built':
            raise ValueError(f'Release target is blocked, skipped or unsigned: {name}')
    rebuilt = candidate.assemble(root, source_revision, 'built', source_tree, 'all', {
        key: manifest.get(key) for key in ('workflow_revision', 'workflow_run_id', 'workflow_run_attempt')})
    if rebuilt['artifacts'] != manifest.get('artifacts') or rebuilt['targets'] != manifest['targets']:
        raise ValueError('Candidate payload/status changed after manifest creation')

    windows = root / 'windows'
    metadata = json.loads((windows / 'windows-package.json').read_text())
    installer = metadata.get('installer', {})
    exe = windows / 'habab_home_assistant.exe'
    if (installer.get('filename') != exe.name or installer.get('source_revision') != source_revision
            or installer.get('source_dirty') is not False
            or digest(exe) != (installer.get('sha256'), installer.get('bytes'))):
        raise ValueError('Windows EXE metadata does not attest this exact installer/source')
    with exe.open('rb') as stream:
        if stream.read(2) != b'MZ':
            raise ValueError('Windows installer is not a PE executable')

    android = root / 'android'
    status = manifest['targets']['android']
    if status.get('apk_signature') != 'verified-release' or status.get('publishable') is not True:
        raise ValueError('Android APK lacks successful release signature verification')
    runtime_url = status.get('runtime_manifest', '')
    if not runtime_url.startswith(base + '/') or '/' in runtime_url[len(base) + 1:]:
        raise ValueError('APK runtime URL does not refer to this GitHub Release')
    runtime_manifest = android / Path(urlsplit(runtime_url).path).name
    release = json.loads(runtime_manifest.read_text())
    if release.get('sourceRevision') != source_revision:
        raise ValueError('Android runtime source differs from this release')
    archive_url = release.get('url', '')
    if not archive_url.startswith(base + '/') or '/' in archive_url[len(base) + 1:]:
        raise ValueError('Android runtime archive is outside this GitHub Release')
    archive = android / Path(urlsplit(archive_url).path).name
    if digest(archive) != (release.get('sha256'), release.get('bytes')):
        raise ValueError('Android runtime checksum differs from signed manifest')
    verifier_spec = importlib.util.spec_from_file_location('runtime_verifier', source_tree / 'habab/android/tools/verify_runtime.py')
    verifier = importlib.util.module_from_spec(verifier_spec)
    verifier_spec.loader.exec_module(verifier)
    signature = verifier.decode_signature(Path(str(runtime_manifest) + '.sig'))
    public_key = source_tree / 'habab/android/app/src/main/assets/release/trusted-key.der'
    subprocess.run(['openssl', 'dgst', '-sha256', '-verify', str(public_key), '-keyform', 'DER',
                    '-signature', '/dev/stdin', str(runtime_manifest)], input=signature, check=True,
                   capture_output=True)
    frontend_pin = json.loads((source_tree / 'habab/distribution/native-frontend-release.json').read_text())
    pinned_wheel_sha = frontend_pin.get('sha256')
    if (not isinstance(pinned_wheel_sha, str) or not re.fullmatch(r'[0-9a-f]{64}', pinned_wheel_sha)
            or release.get('nativeFrontend', {}).get('wheelSha256') != pinned_wheel_sha):
        raise ValueError('Windows/Linux frontend wheel SHA-256 differs from signed Android runtime')
    apk = android / 'habab-home-arm64.apk'
    verifier.verify_apk_provenance(apk, release, runtime_manifest)
    with zipfile.ZipFile(apk) as package:
        if any('/voice/experiments/' in name or name.lower().endswith(('.wav', '.tflite', '.onnx'))
               for name in package.namelist()):
            raise ValueError('Public APK contains private experimental model/audio assets')

    selected = [exe, windows / metadata['filename'], windows / 'windows-package.json', apk,
                runtime_manifest, Path(str(runtime_manifest) + '.sig'), archive]
    for arch in ('amd64', 'arm64'):
        image = root / f'linux-{arch}' / f'habab-home-linux-{arch}.oci.tar.gz'
        candidate.verify_linux_image(image, f'linux/{arch}', source_revision)
        selected.append(image)
    if output.exists() and any(output.iterdir()):
        raise ValueError('Release staging directory must be empty')
    output.mkdir(parents=True, exist_ok=True)
    assets = []
    for path in selected:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'Release asset is missing or is a symlink: {path}')
        sha, size = digest(path)
        if not 0 < size <= MAX_ASSET_BYTES:
            raise ValueError(f'Release asset exceeds GitHub limit or is empty: {path.name}')
        target = output / path.name
        if target.exists():
            raise ValueError('Release asset name collision')
        # Hard links avoid a second multi-gigabyte copy on the same runner.
        try:
            target.hardlink_to(path)
        except OSError:
            shutil.copyfile(path, target)
        assets.append({'name': path.name, 'sha256': sha, 'bytes': size,
                       'url': base + '/' + path.name})
    receipt = {'schema': 1, 'repository': repository, 'tag': tag, 'source_revision': source_revision,
               'source_dirty': False, 'targets': manifest['targets'], 'assets': assets,
               'workflow_revision': manifest['workflow_revision'], 'workflow_run_id': manifest['workflow_run_id'],
               'workflow_run_attempt': manifest['workflow_run_attempt']}
    receipt['windows_linux_frontend_dependency'] = frontend_pin
    (output / 'release-receipt.json').write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
    sha, size = digest(output / 'release-receipt.json')
    assets.append({'name': 'release-receipt.json', 'sha256': sha, 'bytes': size})
    (output / 'SHA256SUMS').write_text(''.join(f"{item['sha256']}  {item['name']}\n" for item in assets))
    sha, size = digest(output / 'SHA256SUMS')
    assets.append({'name': 'SHA256SUMS', 'sha256': sha, 'bytes': size})
    # Local verification receipt includes its own auxiliary files without a recursive self-hash.
    return {**receipt, 'assets': assets}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--source-tree', type=Path, default=Path('.'))
    parser.add_argument('--source-revision')
    parser.add_argument('--workflow-revision')
    parser.add_argument('--repository')
    parser.add_argument('--tag')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--verify-uploaded', type=Path)
    args = parser.parse_args()
    if args.verify_uploaded:
        verify_uploaded(json.loads(args.receipt.read_text()), json.loads(args.verify_uploaded.read_text()))
    else:
        args.receipt.write_text(json.dumps(stage(args.root, args.source_tree, args.source_revision,
                                                args.repository, args.tag, args.output, args.workflow_revision), indent=2) + '\n')


if __name__ == '__main__':
    main()
