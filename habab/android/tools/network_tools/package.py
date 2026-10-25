"""Install opt-in network tools from signed, pinned upstream artifacts."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import urllib.request
import uuid
from zipfile import ZipFile

TOOLS = Path(__file__).resolve().parent
LOCK_PATH = TOOLS / 'network-tools.lock.json'
MARKER = Path('etc/habab-android-network-tools.json')
APK_HELPER = 'ghcr.io/home-assistant/home-assistant:2026.10.2@sha256:33afb0659d786213264c2211f8d91c5fe87461c56eba6a3a546eb05fc71e89f4'


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def load_lock():
    lock = json.loads(LOCK_PATH.read_text())
    if lock.get('schema') != 1:
        raise ValueError('Unsupported network-tools lock schema')
    if lock['kurigram']['sha256'] != 'f53cee6119a579b74620f45b5f83d543cd3bd510a8a6a1b4b3852e21603241e9':
        raise ValueError('Unexpected Kurigram wheel digest pin')
    return lock


def _installed_version(site, distribution):
    normalized = distribution.lower().replace('-', '_').replace('.', '_')
    matches = list(site.glob(normalized + '-*.dist-info/METADATA'))
    if len(matches) != 1:
        raise ValueError('Expected one installed Core dependency: ' + distribution)
    for line in matches[0].read_text().splitlines():
        if line.lower().startswith('version:'):
            return line.split(':', 1)[1].strip()
    raise ValueError('Installed dependency metadata has no version: ' + distribution)


def _install_wheel(rootfs, cache, lock):
    record = lock['kurigram']
    wheelhouse = cache / 'network-tools'
    wheelhouse.mkdir(parents=True, exist_ok=True)
    wheel = wheelhouse / record['filename']
    if not wheel.exists():
        partial = wheel.with_suffix(wheel.suffix + '.partial')
        try:
            urllib.request.urlretrieve(record['url'], partial)
            if sha256(partial) != record['sha256']:
                raise ValueError('Kurigram wheel checksum mismatch')
            partial.replace(wheel)
        finally:
            partial.unlink(missing_ok=True)
    if wheel.stat().st_size != record['bytes'] or sha256(wheel) != record['sha256']:
        raise ValueError('Cached Kurigram wheel checksum mismatch')

    site = rootfs / ('usr/local/lib/python' + record['python'] + '/site-packages')
    if not site.is_dir():
        raise ValueError('Pinned Kurigram wheel requires the Python 3.14 Core runtime')
    dependencies = {name: _installed_version(site, name)
                    for name in record['coreDependencies']}
    if dependencies != record['coreDependencies']:
        raise ValueError('Kurigram Core dependency pins do not match the base runtime')

    target = rootfs / 'opt/habab-network-tools/python'
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    with ZipFile(wheel) as archive:
        entries = archive.infolist()
        metadata_name = 'kurigram-' + record['version'] + '.dist-info/METADATA'
        if metadata_name not in {entry.filename for entry in entries}:
            raise ValueError('Kurigram wheel metadata is missing')
        for entry in entries:
            path = Path(entry.filename)
            if path.is_absolute() or '..' in path.parts:
                raise ValueError('Unsafe path in Kurigram wheel')
            if not (entry.filename.startswith('pyrogram/')
                    or entry.filename.startswith('kurigram-' + record['version'] + '.dist-info/')):
                continue
            destination = target / path
            if entry.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, destination.open('wb') as output:
                    shutil.copyfileobj(source, output)
                destination.chmod(0o755 if entry.external_attr >> 16 & 0o111 else 0o644)
    return wheel, dependencies, target


def _reuse_chromium_transaction(rootfs, apk_cache, receipt_path, lock):
    """Accept an existing pinned APK transaction without refreshing its cache."""
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise ValueError('Cached APK transaction receipt is not a regular file')
    transaction = json.loads(receipt_path.read_text())
    chromium_pin = lock['alpine']['chromium']
    chromium_record = transaction.get('chromiumPackage', {})
    if (transaction.get('transactionMethod') != 'apk --root /target --arch aarch64 add'
            or transaction.get('packageScripts') != 'disabled'
            or not str(transaction.get('packageHooksValidation', '')).startswith('NOT_RUN:')
            or chromium_record.get('version') != chromium_pin['version']
            or chromium_record.get('architecture') != lock['alpine']['architecture']
            or chromium_record.get('apkControlHash') != chromium_pin['apkControlHash']
            or chromium_record.get('installedBytes') != chromium_pin['installedBytes']):
        raise ValueError('Cached APK transaction does not match the pinned Chromium contract')

    expected_indexes = set(lock['alpine']['apkIndexesSha256'].values())
    index_files = list(apk_cache.glob('APKINDEX.*.tar.gz'))
    if ({sha256(path) for path in index_files} != expected_indexes
            or len(index_files) != len(expected_indexes)):
        raise ValueError('Cached APK indexes do not match the pinned snapshot')

    database = rootfs / 'lib/apk/db/installed'
    if (not database.is_file()
            or sha256(database) != transaction.get('installedDatabaseAfterSha256')):
        raise ValueError('Cached APK database does not match its transaction receipt')
    records = {}
    for block in database.read_text().split('\n\n'):
        fields = {line[0]: line[2:] for line in block.splitlines()
                  if len(line) > 2 and line[1] == ':'}
        if fields.get('P'):
            records[fields['P']] = fields
    chromium = records.get(chromium_pin['package'])
    if (not chromium
            or chromium.get('V') != chromium_pin['version']
            or chromium.get('A') != lock['alpine']['architecture']
            or chromium.get('C') != chromium_pin['apkControlHash']):
        raise ValueError('Cached APK database has no pinned Chromium package record')

    changes = transaction.get('packageChanges')
    if not isinstance(changes, list):
        raise ValueError('Cached APK transaction package delta is missing')
    for change in changes:
        name = change.get('name')
        record = records.get(name)
        if change.get('removed'):
            if record is not None:
                raise ValueError('Cached APK package delta disagrees with its database')
        elif (not record
              or record.get('V') != change.get('version')
              or record.get('A') != change.get('architecture')
              or record.get('C') != change.get('apkControlHash')):
            raise ValueError('Cached APK package delta disagrees with its database')

    chromium_binary = rootfs / 'usr/lib/chromium/chromium'
    chromium_launcher = rootfs / 'usr/bin/chromium'
    if (not chromium_binary.is_file()
            or not chromium_launcher.exists()
            or sha256(chromium_binary) != transaction.get('chromiumExecutableSha256')):
        raise ValueError('Cached Chromium executable does not match its transaction receipt')
    return transaction, chromium_binary


def _fetch_chromium(rootfs, cache, helper_image, lock):
    release_file = rootfs / 'etc/alpine-release'
    if not release_file.is_file() or release_file.read_text().strip() != lock['alpine']['release']:
        raise ValueError('Pinned Chromium requires Alpine ' + lock['alpine']['release'])
    apk_cache = cache / 'network-tools' / 'apk-cache'
    apk_cache.mkdir(parents=True, exist_ok=True)
    receipt_path = apk_cache / 'transaction.json'
    helper_script = TOOLS / 'fetch_chromium.sh'
    if not (rootfs / 'etc/apk/repositories').is_file() or not (rootfs / 'etc/apk/keys').is_dir():
        raise ValueError('Alpine package sources or trust keys are missing')
    if not (rootfs / 'lib/apk/db/installed').is_file():
        raise ValueError('Expected the existing Alpine package database')
    if receipt_path.exists() or receipt_path.is_symlink():
        return _reuse_chromium_transaction(rootfs, apk_cache, receipt_path, lock)
    helper_name = 'habab-network-tools-' + uuid.uuid4().hex
    try:
        subprocess.run([
            'docker', 'run', '--rm', '--name', helper_name, '--platform', 'linux/amd64',
            '--entrypoint', '/bin/sh',
            '-v', str(rootfs) + ':/target',
            '-v', str(apk_cache) + ':/apk-cache',
            '-v', str(LOCK_PATH) + ':/network-tools.lock.json:ro',
            '-v', str(helper_script) + ':/fetch-chromium.sh:ro',
            helper_image, '/fetch-chromium.sh', '/network-tools.lock.json',
            '/target', '/apk-cache', '/apk-cache/transaction.json',
        ], check=True, timeout=300)
    finally:
        # --rm handles the success path; this exact name also cleans a helper
        # left behind if the CLI times out or is interrupted.
        try:
            cleanup = subprocess.run(['docker', 'rm', '-f', helper_name], check=False,
                                      capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            cleanup = subprocess.run(['docker', 'rm', '-f', helper_name], check=False,
                                     capture_output=True, text=True, timeout=30)
        if cleanup.returncode and 'No such container' not in cleanup.stderr:
            raise RuntimeError('Unable to remove the network-tools helper container ' + helper_name)
    if not receipt_path.is_file():
        raise ValueError('Pinned Alpine transaction receipt is missing')
    transaction = json.loads(receipt_path.read_text())
    chromium_package = transaction.get('chromiumPackage', {})
    if (chromium_package.get('version') != lock['alpine']['chromium']['version']
            or chromium_package.get('architecture') != lock['alpine']['architecture']
            or chromium_package.get('apkControlHash') != lock['alpine']['chromium']['apkControlHash']):
        raise ValueError('Pinned Chromium was not registered by the APK transaction')
    chromium = rootfs / 'usr/lib/chromium/chromium'
    launcher = rootfs / 'usr/bin/chromium'
    if not chromium.is_file() or not launcher.exists():
        raise ValueError('Pinned Chromium package did not provide its real browser executable')
    if transaction.get('chromiumExecutableSha256') != sha256(chromium):
        raise ValueError('Chromium transaction receipt executable hash mismatch')
    return transaction, chromium


def _payload(target):
    inventory = {str(path.relative_to(target)): sha256(path)
                 for path in sorted(target.rglob('*')) if path.is_file()}
    encoded = json.dumps(inventory, sort_keys=True, separators=(',', ':')).encode()
    return len(inventory), hashlib.sha256(encoded).hexdigest()


def install(rootfs, cache, helper_image=APK_HELPER):
    lock = load_lock()
    lock_sha256 = sha256(LOCK_PATH)
    marker_path = rootfs / MARKER
    pending_path = rootfs / 'etc/habab-android-network-tools.installing'
    if marker_path.is_file():
        installed = json.loads(marker_path.read_text())
        if installed.get('lockSha256') != lock_sha256:
            raise ValueError('Cached runtime contains network tools from a different lock; rebuild its rootfs')
        if (installed.get('browserLauncherSha256') != sha256(TOOLS / 'habab-browser')
                or installed.get('pythonLauncherSha256') != sha256(TOOLS / 'habab-python-network')):
            raise ValueError('Network-tools launcher templates changed; refresh the runtime builder input')
        chromium = rootfs / 'usr/lib/chromium/chromium'
        package_database = rootfs / 'lib/apk/db/installed'
        browser_launcher = rootfs / 'usr/local/bin/habab-browser'
        chromium_command = rootfs / 'usr/local/bin/chromium'
        python_launcher = rootfs / 'usr/local/bin/habab-python-network'
        payload = rootfs / 'opt/habab-network-tools/python'
        count, payload_sha256 = _payload(payload)
        expected_database = installed.get('installedDatabaseSha256',
                                         installed.get('apkTransaction', {}).get('installedDatabaseAfterSha256'))
        if (not chromium.is_file()
                or sha256(chromium) != installed.get('chromiumExecutableSha256')
                or not package_database.is_file()
                or sha256(package_database) != expected_database
                or not browser_launcher.is_file()
                or sha256(browser_launcher) != installed.get('browserLauncherSha256')
                or not chromium_command.is_symlink()
                or os.readlink(chromium_command) != 'habab-browser'
                or not python_launcher.is_file()
                or sha256(python_launcher) != installed.get('pythonLauncherSha256')
                or count != installed.get('pythonPayloadFiles')
                or payload_sha256 != installed.get('pythonPayloadSha256')):
            raise ValueError('Cached network-tools payload does not match its install receipt')
        return installed

    if helper_image != APK_HELPER:
        raise ValueError('Unexpected Alpine APK resolver image pin')
    if pending_path.is_file() and pending_path.read_text().strip() != lock_sha256:
        raise ValueError('Interrupted network-tools install used a different lock; rebuild its rootfs')
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    pending_path.write_text(lock_sha256 + '\n')
    transaction, chromium = _fetch_chromium(rootfs, cache, helper_image, lock)
    wheel, dependencies, target = _install_wheel(rootfs, cache, lock)

    browser_launcher = rootfs / 'usr/local/bin/habab-browser'
    chromium_command = rootfs / 'usr/local/bin/chromium'
    python_launcher = rootfs / 'usr/local/bin/habab-python-network'
    for source, destination in ((TOOLS / 'habab-browser', browser_launcher),
                                (TOOLS / 'habab-python-network', python_launcher)):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destination.chmod(0o755)
    if chromium_command.exists() or chromium_command.is_symlink():
        if not chromium_command.is_symlink() or os.readlink(chromium_command) != 'habab-browser':
            raise ValueError('Unexpected pre-existing Chromium command path')
    else:
        chromium_command.symlink_to('habab-browser')

    payload_files, payload_sha256 = _payload(target)
    installed = {
        'lockSha256': lock_sha256,
        'alpineRelease': lock['alpine']['release'],
        'architecture': lock['alpine']['architecture'],
        'apkIndexesSha256': lock['alpine']['apkIndexesSha256'],
        'chromiumVersion': lock['alpine']['chromium']['version'],
        'chromiumExecutableSha256': sha256(chromium),
        'apkTransaction': transaction,
        'installedDatabaseSha256': transaction.get('installedDatabaseAfterSha256'),
        'kurigramVersion': lock['kurigram']['version'],
        'kurigramWheelSha256': sha256(wheel),
        'coreDependencyVersions': dependencies,
        'pythonPayloadFiles': payload_files,
        'pythonPayloadSha256': payload_sha256,
        'browserLauncherSha256': sha256(browser_launcher),
        'chromiumCommand': 'chromium -> habab-browser',
        'pythonLauncherSha256': sha256(python_launcher),
    }
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(json.dumps(installed, sort_keys=True, indent=2) + '\n')
    pending_path.unlink(missing_ok=True)
    return installed


def finalize_package_database(rootfs, installed, node_packages):
    """Record the final APK DB after the pinned Node transaction has run."""
    marker_path = rootfs / MARKER
    if not marker_path.is_file():
        raise ValueError('Network-tools install receipt is missing before final APK database recording')
    marker = json.loads(marker_path.read_text())
    lock_sha256 = sha256(LOCK_PATH)
    if marker.get('lockSha256') != lock_sha256 or installed.get('lockSha256') != lock_sha256:
        raise ValueError('Network-tools install receipt lock changed before final APK database recording')
    database = rootfs / 'lib/apk/db/installed'
    if not database.is_file():
        raise ValueError('Final APK database is missing')
    digest = sha256(database)
    transaction_hash = marker.get('apkTransaction', {}).get('installedDatabaseAfterSha256')
    if (not transaction_hash
            or installed.get('apkTransaction', {}).get('installedDatabaseAfterSha256') != transaction_hash):
        raise ValueError('Chromium transaction database hash is missing')
    node_lock = json.loads((TOOLS / 'node-apks.lock.json').read_text())
    expected_node = node_lock['packages']['nodejs-' + node_lock['nodeVersion'] + '.apk']['sha256']
    if node_packages.get('nodejs-' + node_lock['nodeVersion'] + '.apk') != expected_node:
        raise ValueError('Final APK database lacks the pinned Node package inventory')
    records = {}
    for block in database.read_text().split('\n\n'):
        fields = {line[0]: line[2:] for line in block.splitlines()
                  if len(line) > 2 and line[1] == ':'}
        if fields.get('P'):
            records[fields['P']] = fields
    node = records.get('nodejs', {})
    chromium = records.get('chromium', {})
    if (node.get('V') != node_lock['nodeVersion'] or node.get('A') != node_lock['architecture']
            or chromium.get('V') != installed.get('chromiumVersion')):
        raise ValueError('Final APK database does not contain pinned Node and Chromium packages')
    inventory_hash = hashlib.sha256(json.dumps(
        node_packages, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    marker['installedDatabaseSha256'] = digest
    marker['nodePackagesSha256'] = inventory_hash
    marker_path.write_text(json.dumps(marker, sort_keys=True, indent=2) + '\n')
    installed['installedDatabaseSha256'] = digest
    installed['nodePackagesSha256'] = inventory_hash
    return installed
