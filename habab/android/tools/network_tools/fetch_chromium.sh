#!/bin/sh
set -eu

lock_file="$1"
target_root="$2"
cache_dir="$3"
receipt_file="$4"

mkdir -p "$cache_dir"
apk --root "$target_root" --arch aarch64 --cache-dir "$cache_dir" update
python3 - "$lock_file" "$target_root" "$cache_dir" <<'PY'
import hashlib
import json
from pathlib import Path
import sys
import tarfile

lock = json.loads(Path(sys.argv[1]).read_text())
root = Path(sys.argv[2])
cache = Path(sys.argv[3])
expected = set(lock['alpine']['apkIndexesSha256'].values())
indexes = list(cache.glob('APKINDEX.*.tar.gz'))
actual = {hashlib.sha256(index.read_bytes()).hexdigest() for index in indexes}
if actual != expected:
    raise SystemExit('Alpine APKINDEX snapshot hash mismatch')

community = next(index for index in indexes
                 if hashlib.sha256(index.read_bytes()).hexdigest()
                 == lock['alpine']['apkIndexesSha256']['community'])
with tarfile.open(community, 'r:gz') as archive:
    records = archive.extractfile('APKINDEX').read().decode().split('\n\n')
chromium = {}
for record in records:
    fields = {line[0]: line[2:] for line in record.splitlines()
              if len(line) > 2 and line[1] == ':'}
    if fields.get('P') == lock['alpine']['chromium']['package']:
        chromium = fields
        break
expected_package = lock['alpine']['chromium']
if (chromium.get('V') != expected_package['version']
        or chromium.get('A') != lock['alpine']['architecture']
        or chromium.get('C') != expected_package['apkControlHash']
        or int(chromium.get('S', '-1')) != expected_package['compressedBytes']
        or int(chromium.get('I', '-1')) != expected_package['installedBytes']):
    raise SystemExit('Pinned Chromium package metadata mismatch')

repositories = {
    line.strip()
    for line in (root / 'etc/apk/repositories').read_text().splitlines()
    if line.strip() and not line.lstrip().startswith('#')
}
if repositories != set(lock['alpine']['repositories'].values()):
    raise SystemExit('Unexpected Alpine repository configuration')
if not (root / 'lib/apk/db/installed').is_file():
    raise SystemExit('Expected an existing Alpine package database')
PY

python3 - "$target_root/lib/apk/db/installed" "$cache_dir/installed-before" <<'PY'
from pathlib import Path
import sys
Path(sys.argv[2]).write_bytes(Path(sys.argv[1]).read_bytes())
PY

version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["alpine"]["chromium"]["version"])' "$lock_file")"
# This is a foreign aarch64 root on x86_64 with no qemu binfmt. Chromium's
# Alpine APKBUILD declares runtime dependencies for --headless; desktop
# integration hooks are unnecessary here. Fontconfig can scan its pinned font
# files on demand. Do not use --force-no-chroot or run guest scripts on host.
apk --root "$target_root" --arch aarch64 --cache-dir "$cache_dir" --no-scripts \
  add "chromium=$version"

python3 - "$target_root/lib/apk/db/installed" "$cache_dir/installed-before" \
  "$receipt_file" "$lock_file" "$target_root/usr/lib/chromium/chromium" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

def packages(raw):
    result = {}
    for block in raw.decode().split('\n\n'):
        fields = {line[0]: line[2:] for line in block.splitlines()
                  if len(line) > 2 and line[1] == ':'}
        name = fields.get('P')
        if name:
            result[name] = fields
    return result

def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()

db = Path(sys.argv[1]).read_bytes()
before = packages(Path(sys.argv[2]).read_bytes())
after = packages(db)
lock = json.loads(Path(sys.argv[4]).read_text())
changed = []
for name in sorted(set(before) | set(after)):
    old = before.get(name)
    new = after.get(name)
    if old == new:
        continue
    if new is None:
        changed.append({'name': name, 'fromVersion': old.get('V'), 'removed': True})
    else:
        changed.append({
            'name': name,
            'fromVersion': old.get('V') if old else None,
            'version': new.get('V'),
            'architecture': new.get('A'),
            'apkControlHash': new.get('C'),
            'installedBytes': int(new['I']) if new.get('I', '').isdigit() else None,
            'removed': False,
        })
chromium = after.get('chromium')
if (not chromium or chromium.get('V') != lock['alpine']['chromium']['version']
        or chromium.get('A') != 'aarch64'):
    raise SystemExit('APK transaction did not register pinned aarch64 Chromium')
binary = Path(sys.argv[5])
if not binary.is_file():
    raise SystemExit('APK transaction did not install the Chromium executable')
receipt = {
    'transactionMethod': 'apk --root /target --arch aarch64 add',
    'packageScripts': 'disabled',
    'packageHooksValidation': 'NOT_RUN: package script effects were not exercised for the headless candidate',
    'packageChanges': changed,
    'chromiumPackage': {
        'version': chromium.get('V'),
        'architecture': chromium.get('A'),
        'apkControlHash': chromium.get('C'),
        'installedBytes': int(chromium['I']) if chromium.get('I', '').isdigit() else None,
    },
    'installedDatabaseBeforeSha256': file_sha(sys.argv[2]),
    'installedDatabaseAfterSha256': hashlib.sha256(db).hexdigest(),
    'chromiumExecutableSha256': file_sha(binary),
}
Path(sys.argv[3]).write_text(json.dumps(receipt, sort_keys=True, indent=2) + '\n')
PY
