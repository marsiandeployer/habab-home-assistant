"""Compare the final built/running image and prove the binary mount is read-only."""
import hashlib
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[2]
source = root / 'habab/distribution/src/habab_home'
container = 'habab-setup-agent-qa'
inspection = json.loads(subprocess.check_output(['docker', 'inspect', container]))[0]
image = json.loads(subprocess.check_output(['docker', 'image', 'inspect', 'habab-home:opencode-20261022']))[0]
assert inspection['Image'] == image['Id']
manifest = {}
for path in source.rglob('*'):
    if not path.is_file() or path.suffix not in {'.py', '.js', '.json'}:
        continue
    name = path.relative_to(source).as_posix()
    built = subprocess.check_output(['docker', 'exec', container, 'cat', '/usr/src/homeassistant/homeassistant/components/habab_home/' + name])
    assert hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(built).digest(), name
    manifest[name] = hashlib.sha256(built).hexdigest()
helper = subprocess.check_output(['docker', 'exec', container, 'cat', '/usr/local/bin/habab-ha'])
assert helper == (root / 'habab/distribution/habab-ha.py').read_bytes()
mount = next(m for m in inspection['Mounts'] if m['Destination'] == '/opt/habab-opencode/opencode')
assert not mount['RW'] and mount['Type'] == 'bind'
assert not inspection['HostConfig']['Privileged']
assert all(m['Destination'] not in {'/var/run/docker.sock', '/root', '/'} for m in inspection['Mounts'])
mountinfo = subprocess.check_output(['docker', 'exec', container, 'cat', '/proc/self/mountinfo'], text=True)
row = next(line.split() for line in mountinfo.splitlines() if line.split()[4] == '/opt/habab-opencode/opencode')
assert 'ro' in row[5].split(',')
assert (source / 'frontend/setup-chat.js').read_bytes() == (root / 'habab/public/integrations/setup-chat.js').read_bytes()
report = {'status': 'PASS', 'image': inspection['Image'], 'source_files_matched': len(manifest), 'files': manifest,
          'native_cli_matches': True, 'executable_read_only': True, 'kernel_mount_options': row[5],
          'no_privileged': True, 'no_docker_socket': True, 'public_module_matches_source': True}
(root / 'habab/qa/setup-agent/build-identity.json').write_text(json.dumps(report, indent=2) + '\n')
print('Built image, CLI and read-only binary: PASS')
