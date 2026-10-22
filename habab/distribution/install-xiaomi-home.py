#!/usr/bin/env python3
"""Install the owner's optional official Xiaomi component from its pinned source.

The Xiaomi code is not bundled or redistributed in Habab images or Windows kits.
This command is for the owner's personal non-commercial Home Assistant instance.
"""
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import urllib.request
import zipfile


def install(config=Path('/config'), release_file=None):
    release_file = release_file or Path(__file__).with_name('xiaomi-home-release.json')
    release = json.loads(release_file.read_text())
    destination = config / 'custom_components' / 'xiaomi_home'
    marker = destination / 'habab-source.json'
    if destination.exists():
        if marker.is_file() and json.loads(marker.read_text()).get('sha256') == release['sha256']:
            return {'installed': True, 'version': release['version'], 'already_installed': True}
        raise ValueError('Existing Xiaomi component retained; update it through its own installation method')
    url = release['url']
    expected = 'https://codeload.github.com/XiaoMi/ha_xiaomi_home/zip/' + release['commit']
    if url != expected or len(release['sha256']) != 64:
        raise ValueError('Invalid pinned source')
    with urllib.request.urlopen(url, timeout=30) as response:
        data = response.read(4_000_001)
    if len(data) > 4_000_000 or hashlib.sha256(data).hexdigest() != release['sha256']:
        raise ValueError('Official source integrity check failed')
    destination.parent.mkdir(parents=True, exist_ok=True)
    prefix = 'ha_xiaomi_home-' + release['commit'] + '/'
    component_prefix = prefix + 'custom_components/xiaomi_home/'
    with tempfile.TemporaryDirectory(prefix='.habab-xiaomi-', dir=destination.parent) as work:
        staging = Path(work) / 'xiaomi_home'
        staging.mkdir()
        with zipfile.ZipFile(io.BytesIO(data)) as source:
            total = 0
            for entry in source.infolist():
                if not entry.filename.startswith(component_prefix) or entry.is_dir():
                    continue
                relative = PurePosixPath(entry.filename[len(component_prefix):])
                if relative.is_absolute() or '..' in relative.parts or '\\' in str(relative) or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError('Unsafe archive member')
                total += entry.file_size
                if total > 20_000_000:
                    raise ValueError('Official component too large')
                target = staging.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read(entry))
            for name in ('LICENSE.md', 'LegalNotice.md'):
                (staging / name).write_bytes(source.read(prefix + name))
        manifest = json.loads((staging / 'manifest.json').read_text())
        if manifest.get('domain') != 'xiaomi_home' or manifest.get('version') != release['version']:
            raise ValueError('Unexpected official component manifest')
        (staging / 'habab-source.json').write_text(json.dumps(release, indent=2) + '\n')
        os.rename(staging, destination)
    return {'installed': True, 'version': release['version'], 'restart_required': True,
            'login': 'Official Xiaomi OAuth; passwords are not passed to the AI assistant'}


if __name__ == '__main__':
    try:
        print(json.dumps(install()))
    except Exception as error:
        print(json.dumps({'installed': False, 'error': type(error).__name__}))
        raise SystemExit(1) from None
