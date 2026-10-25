#!/usr/bin/env python3
"""Publish a verified immutable runtime, then retain current and previous."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from verify_runtime import verify

HABAB = Path(__file__).resolve().parents[2]


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def retention_preview(state, retention):
    result = subprocess.run([sys.executable, str(retention), '--state', str(state)],
                            check=True, capture_output=True, text=True, timeout=30)
    preview = json.loads(result.stdout)
    if preview.get('status') != 'PREVIEW':
        raise ValueError('release retention state is not ready: ' + str(preview.get('reason', 'invalid preview')))
    return preview


def publish(manifest, public_key, destination, state, retention, source_revision):
    manifest, public_key = Path(manifest).resolve(), Path(public_key).resolve()
    destination, state, retention = Path(destination).resolve(), Path(state).resolve(), Path(retention).resolve()
    if not re.fullmatch(r'[0-9a-f]{40}', source_revision):
        raise ValueError('source-revision must be a full 40-character lowercase commit SHA')
    if not state.is_file() or not retention.is_file():
        raise ValueError('initialize the current/previous retention manifest before publication')
    record = json.loads(manifest.read_text())
    if record.get('sourceRevision') != source_revision:
        raise ValueError('runtime sourceRevision does not match the expected frozen checkout')
    identity = record['id']
    if not re.fullmatch(r'[a-f0-9]{16}', identity):
        raise ValueError('invalid runtime ID')
    archive_name = f'habab-runtime-{identity}-arm64.tgz'
    if manifest.name != f'release-{identity}.json' or record['url'].rsplit('/', 1)[-1] != archive_name:
        raise ValueError('runtime names differ from the signed manifest')
    sources = [manifest.parent / archive_name, Path(str(manifest) + '.sig'), manifest]
    # Confirm the retention state can keep current and previous before creating
    # a public destination or moving any part of the bundle.
    retention_preview(state, retention)
    destination.mkdir(parents=True, exist_ok=True)
    with state.with_suffix('.publish.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ledger = json.loads(state.read_text())
        root = Path(ledger['root']).resolve()
        artifacts = sorted({str(p.relative_to(root)) for source in sources
                            for p in (source, destination / source.name)})
        with tempfile.TemporaryDirectory(prefix='.runtime-publish-', dir=destination) as directory:
            stage = Path(directory)
            for source in sources:
                if source.is_symlink() or not source.is_file():
                    raise ValueError('runtime source must be a regular file')
                try:
                    os.link(source, stage / source.name)
                except OSError:
                    shutil.copy2(source, stage / source.name)
            # Signature, archive hash and contents are checked before any
            # publication or retention state change.
            verify(stage / manifest.name, public_key, source_revision)
            for source in sources:
                target = destination / source.name
                if target.exists() and digest(target) != digest(stage / source.name):
                    raise ValueError('refusing to replace an immutable published artifact')
            for source in sources:
                target = destination / source.name
                if not target.exists():
                    os.replace(stage / source.name, target)
        command = [sys.executable, str(retention), '--state', str(state),
                   '--release', identity, '--apply']
        for value in artifacts:
            command.extend(['--artifact', value])
        subprocess.run(command, check=True)
    return {'published_runtime': identity, 'destination': str(destination)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--public-key', required=True, type=Path)
    parser.add_argument('--source-revision', required=True,
                        help='Full frozen source commit SHA attested by this runtime')
    parser.add_argument('--destination', type=Path, default=HABAB / 'public/downloads/android')
    parser.add_argument('--state', type=Path, default=Path('/root/tools/release-retention/states/habab-android.json'))
    parser.add_argument('--retention', type=Path, default=Path('/root/tools/release-retention/retention.py'))
    args = parser.parse_args()
    print(json.dumps(publish(args.manifest, args.public_key, args.destination, args.state, args.retention,
                             args.source_revision)))


if __name__ == '__main__':
    main()
