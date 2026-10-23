#!/usr/bin/env python3
"""Ship the native sources, Termux build recipes and our packaging changes."""
import hashlib
import json
from pathlib import Path
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [
    ('proot-5.1.107.99.zip', 'https://github.com/termux/proot/archive/v5.1.107.99.zip',
     '49951a8f35245eaf8e5b300b237ffb10a328f42641a9cbcd1e285d45e95d84e8'),
    ('talloc-2.5.0.tar.gz', 'https://www.samba.org/ftp/talloc/talloc-2.5.0.tar.gz',
     '912afa237510ae542a7733998eb18a12bcda35ab6729c8e2ddb43e8d0ebab007'),
    ('libandroid-shmem-0.7.tar.gz', 'https://github.com/termux/libandroid-shmem/archive/refs/tags/v0.7.tar.gz',
     '1e5ff8459bc0a8c229dd8a94b27d119987e09ef3414331c2b5ebfff20b98e867'),
]


def main():
    cache = ROOT / 'build/sources'; cache.mkdir(parents=True, exist_ok=True)
    inventory = []
    for name, url, digest in SOURCES:
        target = cache / name
        if not target.exists(): urllib.request.urlretrieve(url, target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError('Upstream source checksum mismatch: ' + name)
        inventory.append({'file': name, 'url': url, 'sha256': digest})
    commit_file = cache / 'termux-packages-commit.txt'
    if not commit_file.exists():
        with urllib.request.urlopen('https://api.github.com/repos/termux/termux-packages/commits/master', timeout=30) as response:
            commit = json.load(response)['sha']
        commit_file.write_text(commit + '\n')
    commit = commit_file.read_text().strip()
    recipe_archive = cache / ('termux-packages-' + commit + '.tar.gz')
    if not recipe_archive.exists():
        urllib.request.urlretrieve('https://github.com/termux/termux-packages/archive/' + commit + '.tar.gz', recipe_archive)
    inventory.append({'file': recipe_archive.name, 'commit': commit,
                      'sha256': hashlib.sha256(recipe_archive.read_bytes()).hexdigest()})
    manifest = cache / 'SOURCE-MANIFEST.json'
    manifest.write_text(json.dumps({'nativeSources': inventory,
                                  'nativePackages': json.loads((ROOT / 'native-packages.json').read_text())}, indent=2) + '\n')
    output = ROOT / 'dist/habab-android-sources.tar.gz'
    with tarfile.open(output, 'w:gz', compresslevel=6) as bundle:
        bundle.add(ROOT.parents[1] / 'LICENSE.md', arcname='habab-android/LICENSE.md')
        for path in sorted(cache.iterdir()):
            if path.is_file(): bundle.add(path, arcname='upstream/' + path.name)
        for directory in ('app/src', 'runtime', 'tools', 'tests'):
            for path in sorted((ROOT / directory).rglob('*')):
                if path.is_file() and path.suffix in {'.java', '.xml', '.py', '.md', '.json'} and '__pycache__' not in path.parts:
                    bundle.add(path, arcname='habab-android/' + str(path.relative_to(ROOT)))
        for name in ('build.gradle', 'settings.gradle', 'gradle.properties', 'app/build.gradle', 'README.md', 'NOTICE.md', 'native-packages.json'):
            bundle.add(ROOT / name, arcname='habab-android/' + name)
    print(json.dumps({'sourceBundle': str(output), 'bytes': output.stat().st_size}))


if __name__ == '__main__': main()
