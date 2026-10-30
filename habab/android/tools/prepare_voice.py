#!/usr/bin/env python3
"""Package the pinned offline Russian wake-phrase model, never credentials."""
import hashlib
import json
from pathlib import Path
import shutil
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def main():
    release = json.loads((ROOT / 'voice-model.json').read_text())
    cache = ROOT / 'build/voice'
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / (release['name'] + '.zip')
    if not archive.exists():
        partial = archive.with_suffix('.partial')
        urllib.request.urlretrieve(
            'https://habab-demo.wpmix.net/downloads/android/native-inputs/' + release['sha256'] + '.zip', partial)
        partial.replace(archive)
    if archive.stat().st_size != release['bytes'] or hashlib.sha256(archive.read_bytes()).hexdigest() != release['sha256']:
        raise ValueError('Offline voice model checksum mismatch')
    assets = ROOT / 'app/src/main/assets/voice'
    assets.mkdir(parents=True, exist_ok=True)
    shutil.copy2(archive, assets / 'model.zip')
    shutil.copy2(ROOT / 'voice-model.json', assets / 'model.json')
    print(json.dumps({'model': release['name'], 'bytes': release['bytes'], 'sha256': release['sha256']}))


if __name__ == '__main__':
    main()
