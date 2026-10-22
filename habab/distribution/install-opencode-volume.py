"""Install one pinned Linux OpenCode executable in a dedicated Docker volume.

Run inside the HA image with this directory mounted read-only at /bootstrap and
the named binary volume mounted read-write at /opt/habab-opencode.
"""
import base64
import hashlib
import io
import json
from pathlib import Path
import platform
import tarfile
import urllib.request

arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
if not arch:
    raise SystemExit("Supported architectures: amd64, arm64")
release = json.loads(Path('/bootstrap/opencode-release.json').read_text())[arch]
target = Path('/opt/habab-opencode/opencode')
stamp = target.parent / 'release.json'
saved = json.loads(stamp.read_text()) if stamp.is_file() else {}
current_digest = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
if not (saved.get('release') == release and current_digest == saved.get('sha256')):
    print(f"Downloading pinned OpenCode {release['version']} ({arch})", flush=True)
    with urllib.request.urlopen(release['url'], timeout=90) as response:
        archive = response.read(180_000_001)
    if len(archive) > 180_000_000:
        raise SystemExit("Archive too large")
    expected = release['integrity'].removeprefix('sha512-')
    actual = base64.b64encode(hashlib.sha512(archive).digest()).decode()
    if actual != expected:
        raise SystemExit("OpenCode archive integrity check failed")
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as package:
        member = package.getmember('package/bin/opencode')
        if not member.isfile() or member.size > 250_000_000:
            raise SystemExit("Invalid executable in archive")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix('.next')
        temporary.write_bytes(package.extractfile(member).read())
        temporary.chmod(0o755)
        temporary.replace(target)
with target.open('rb') as binary:
    digest = hashlib.file_digest(binary, 'sha256').hexdigest()
stamp.write_text(json.dumps({'release': release, 'sha256': digest}))
print('HABAB_OPENCODE_SHA256=' + digest)
