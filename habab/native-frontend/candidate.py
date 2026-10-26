"""Validate the local wheel produced by the native frontend candidate job."""
import hashlib
import json
from pathlib import Path
import re


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def load_candidate(directory, source_revision):
    directory = Path(directory)
    manifest_path = directory / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    if (not re.fullmatch(r'[0-9a-f]{40}', source_revision)
            or manifest.get('source_revision') != source_revision
            or manifest.get('source_clean_before_build') is not True
            or manifest.get('builder_checks') != 'passed'):
        raise ValueError('Frontend candidate must be built from the exact clean source revision')
    version = manifest.get('wheel_version', '')
    if not re.fullmatch(r'20260930\.4\+habab\.[0-9]+', version):
        raise ValueError('Invalid candidate frontend version')
    filename = f'home_assistant_frontend-{version}-py3-none-any.whl'
    if manifest.get('artifact') != filename:
        raise ValueError('Unexpected frontend candidate wheel filename')
    wheel = directory / filename
    digest = manifest.get('artifact_sha256', '')
    if (not re.fullmatch(r'[0-9a-f]{64}', digest)
            or type(manifest.get('artifact_bytes')) is not int
            or not 0 < manifest['artifact_bytes'] <= 150_000_000
            or not wheel.is_file() or wheel.is_symlink()
            or wheel.stat().st_size != manifest['artifact_bytes']
            or sha256(wheel) != digest):
        raise ValueError('Frontend candidate wheel integrity mismatch')
    return manifest, wheel, {
        'sourceRevision': source_revision,
        'wheelSha256': digest,
        'version': version,
        'producerManifestSha256': sha256(manifest_path),
    }
