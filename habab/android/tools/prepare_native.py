#!/usr/bin/env python3
"""Package pinned Termux PRoot tools without a com.termux installation.

Only the native tools are reused: APK, service, installer and UI are our own.
Rebase DT_RUNPATH/DT_NEEDED to APK nativeLibraryDir, and override PRoot's
compiled-in loader/temp paths at runtime. Guest programs use the image's musl.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def package_file(name, record, cache, offline=False):
    package = cache / (name + '.deb')
    if not package.exists():
        if offline:
            raise FileNotFoundError(f'Missing offline native package cache entry: {package}')
        package.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(
            'https://packages.termux.dev/apt/termux-main/' + record['Filename'], package)
    if hashlib.sha256(package.read_bytes()).hexdigest() != record['SHA256']:
        raise ValueError(f'Package checksum mismatch: {name}')
    return package


def prepare(package_cache=None):
    offline = package_cache is not None
    cache = ROOT / 'build/native'
    package_cache = Path(package_cache) if offline else cache
    extracted = cache / 'extracted'
    extracted.mkdir(parents=True, exist_ok=True)
    records = json.loads((ROOT / 'native-packages.json').read_text())
    for name, record in records.items():
        package = package_file(name, record, package_cache, offline=offline)
        subprocess.run(['dpkg-deb', '-x', str(package), str(extracted)], check=True)
    prefix = extracted / 'data/data/com.termux/files/usr'
    dest = ROOT / 'app/src/main/jniLibs/arm64-v8a'
    dest.mkdir(parents=True, exist_ok=True)
    sources = {
        'libproot.so': prefix / 'bin/proot',
        'libproot-loader.so': prefix / 'libexec/proot/loader',
    }
    aliases = {}
    for library in sorted((prefix / 'lib').glob('*.so*')):
        if library.is_file():
            target = library.name.split('.so')[0] + '.so'
            sources[target] = library.resolve()
            aliases[library.name] = target
    manifest = {}
    for old in dest.glob('*.so'):
        if old.name not in sources:
            old.unlink()
    for name, source in sources.items():
        target = dest / name
        shutil.copyfile(source, target)
        target.chmod(0o755)
        # Loader is a freestanding static ELF, so it has no dynamic section.
        dynamic = subprocess.run(['patchelf', '--print-needed', str(target)], capture_output=True, text=True)
        if dynamic.returncode == 0:
            subprocess.run(['patchelf', '--set-rpath', '$ORIGIN', str(target)], check=True)
            for dependency in dynamic.stdout.splitlines():
                if dependency in aliases and dependency != aliases[dependency]:
                    subprocess.run(['patchelf', '--replace-needed', dependency, aliases[dependency], str(target)], check=True)
        manifest[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    licenses = ROOT / 'app/src/main/assets/native-licenses'
    if licenses.exists():
        shutil.rmtree(licenses)
    licenses.mkdir(parents=True, exist_ok=True)
    # Keep each upstream package's copyright notices alongside the binaries.
    for path in (prefix / 'share').rglob('*'):
        if path.is_file() and path.name.lower() in ('copyright', 'copying', 'license', 'license.txt'):
            relative = path.relative_to(prefix / 'share')
            target = licenses / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    (cache / 'binaries.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps({'native_tools': len(manifest), 'destination': str(dest),
                      'package_cache': str(package_cache), 'offline_cache': offline}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package-cache', type=Path,
                        help='use only the supplied directory of SHA-pinned .deb inputs; never download')
    prepare(parser.parse_args().package_cache)
