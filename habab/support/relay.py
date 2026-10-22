#!/usr/bin/env python3
"""Approve one requested reverse tunnel; never accept public key enrolment over HTTP."""
import argparse
import base64
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import signal
import socket
import subprocess
import sys
import time

CONFIG = Path('/etc/habab-support')
STATE = Path('/var/lib/habab-support')
PROGRAM = Path('/usr/local/lib/habab-support/relay.py')
PREFIX = 'habab-s-'


def parse_request(value):
    match = re.fullmatch(r'HABAB1:([a-f0-9]{32}):([A-Za-z0-9+/]{68}):([0-9]{1,3})', value.strip())
    if not match:
        raise ValueError('Invalid public support request')
    session, encoded, minutes = match.groups()
    blob = base64.b64decode(encoded, validate=True)
    if len(blob) != 51 or blob[:19] != b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20':
        raise ValueError('Only an Ed25519 public key is accepted')
    minutes = int(minutes)
    if not 5 <= minutes <= 120:
        raise ValueError('A support session must last 5 to 120 minutes')
    return session, encoded, minutes


def session_values(session):
    if not re.fullmatch(r'[a-f0-9]{32}', session):
        raise ValueError('Invalid support session ID')
    return PREFIX + session[:16], 21000 + int(session[:4], 16) % 10000


def atomic_json(path, value, mode=0o600):
    temporary = path.with_name(path.name + '.' + secrets.token_hex(6))
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, 'w') as output:
            json.dump(value, output)
            output.write('\n')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def require_root():
    if os.geteuid() != 0:
        raise ValueError('This command requires the relay administrator')


def install():
    require_root()
    CONFIG.mkdir(mode=0o755, exist_ok=True)
    (CONFIG / 'keys').mkdir(mode=0o755, exist_ok=True)
    (CONFIG / 'sessions').mkdir(mode=0o755, exist_ok=True)
    STATE.mkdir(mode=0o755, exist_ok=True)
    PROGRAM.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if Path(__file__).resolve() != PROGRAM:
        PROGRAM.write_bytes(Path(__file__).read_bytes())
    PROGRAM.chmod(0o755)
    config = Path('/etc/ssh/sshd_config.d/90-habab-support.conf')
    text = '''# Dedicated, expiring Habab support sessions only.
Match User habab-s-*
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AuthorizedKeysFile /etc/habab-support/keys/%u
    AllowTcpForwarding remote
    AllowStreamLocalForwarding no
    GatewayPorts no
    PermitListen 127.0.0.1:*
    PermitOpen none
    AllowAgentForwarding no
    X11Forwarding no
    PermitTTY no
    PermitUserRC no
    MaxSessions 1
Match all
'''
    before = config.read_bytes() if config.exists() else None
    config.write_text(text)
    check = subprocess.run(['/usr/sbin/sshd', '-t'], capture_output=True)
    if check.returncode:
        if before is None:
            config.unlink()
        else:
            config.write_bytes(before)
        raise ValueError('sshd configuration rejected; previous configuration restored')
    subprocess.run(['systemctl', 'reload', 'ssh'], check=True)
    print('Habab relay installed; no client key has been authorized')


def authorize(request):
    require_root()
    session, key, minutes = parse_request(request)
    username, port = session_values(session)
    metadata_path = CONFIG / 'sessions' / (session + '.json')
    if not PROGRAM.is_file() or not (CONFIG / 'keys').is_dir():
        raise ValueError('Install the relay first')
    with (CONFIG / 'authorize.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if metadata_path.exists():
            raise ValueError('This request was already handled; start a new client session')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', port))
        deadline = int(time.time()) + minutes * 60
        directory = STATE / session
        directory.mkdir(mode=0o700)
        subprocess.run(['useradd', '--system', '--user-group', '--no-create-home',
                        '--home-dir', str(directory), '--shell', '/bin/sh',
                        '--comment', 'Habab support ' + session, username], check=True)
        account = pwd.getpwnam(username)
        os.chown(directory, account.pw_uid, account.pw_gid)
        metadata = {'session_id': session, 'username': username, 'uid': account.pw_uid,
                    'port': port, 'deadline': deadline, 'status': 'authorized'}
        atomic_json(metadata_path, metadata)
        expiry = datetime.fromtimestamp(deadline).astimezone().strftime('%Y%m%d%H%M%S')
        command = f'/usr/bin/python3 {PROGRAM} serve --session {session} --deadline {deadline}'
        line = (f'restrict,port-forwarding,permitlisten="127.0.0.1:{port}",'
                f'expiry-time="{expiry}",command="{command}" ssh-ed25519 {key}\n')
        keyfile = CONFIG / 'keys' / username
        try:
            # Install the cutoff before making authentication possible.
            subprocess.run(['systemd-run', '--quiet', '--unit=habab-support-' + session,
                            '--on-active=' + str(minutes * 60) + 's',
                            '--timer-property=AccuracySec=1s', '/usr/bin/python3', str(PROGRAM),
                            'revoke', '--session', session], check=True)
            keyfile.write_text(line)
            keyfile.chmod(0o644)
        except BaseException:
            keyfile.unlink(missing_ok=True)
            subprocess.run(['userdel', username], capture_output=True)
            metadata['status'] = 'authorization_failed'
            atomic_json(metadata_path, metadata)
            raise
        print(json.dumps({'session_id': session, 'username': username, 'port': port,
                          'expires_utc': datetime.fromtimestamp(deadline, timezone.utc).isoformat(),
                          'connection_file': str(directory / 'connection.json')}))


def revoke(session):
    require_root()
    username, _ = session_values(session)
    path = CONFIG / 'sessions' / (session + '.json')
    metadata = json.loads(path.read_text())
    if metadata['username'] != username:
        raise ValueError('Session identity mismatch')
    (CONFIG / 'keys' / username).unlink(missing_ok=True)
    try:
        account = pwd.getpwnam(username)
    except KeyError:
        account = None
    if account:
        if account.pw_uid != metadata['uid'] or account.pw_gecos != 'Habab support ' + session:
            raise ValueError('Refusing to stop an account that does not belong to this session')
        subprocess.run(['pkill', '-TERM', '-u', str(account.pw_uid)], check=False)
        time.sleep(0.3)
        subprocess.run(['pkill', '-KILL', '-u', str(account.pw_uid)], check=False)
        subprocess.run(['userdel', username], check=True)
    (STATE / session / 'connection.json').unlink(missing_ok=True)
    metadata['status'] = 'closed'
    metadata['closed_at'] = datetime.now(timezone.utc).isoformat()
    atomic_json(path, metadata)
    print(json.dumps({'session_id': session, 'status': 'closed', 'home_assistant_data_deleted': False}))


def check_target(port):
    import websocket
    connection = websocket.create_connection(f'ws://127.0.0.1:{port}/api/websocket',
                                              timeout=3, suppress_origin=True)
    try:
        result = json.loads(connection.recv())
        if result.get('type') != 'auth_required' or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+[a-z0-9.]*', result.get('ha_version', '')):
            raise ValueError('The forwarded application is not Home Assistant')
        return result['ha_version']
    finally:
        connection.close()


def serve(session, deadline):
    username, port = session_values(session)
    account = pwd.getpwnam(username)
    if os.geteuid() != account.pw_uid or time.time() >= deadline:
        raise ValueError('Support session is not active')
    directory = STATE / session
    connection_file = directory / 'connection.json'
    lock_file = directory / 'active.lock'
    with lock_file.open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('This support session is already connected') from None
        try:
            version = check_target(port)
            atomic_json(connection_file, {'session_id': session, 'url': f'http://127.0.0.1:{port}',
                        'websocket_url': f'ws://127.0.0.1:{port}/api/websocket', 'ha_version': version,
                        'expires_utc': datetime.fromtimestamp(deadline, timezone.utc).isoformat(),
                        'authentication': 'Native Home Assistant login required'})
            print(f'READY: Home Assistant {version}; support session {session}', flush=True)
            print('Native HA login is still required. Close the Windows support window to disconnect.', flush=True)
            def stop(_signal, _frame):
                raise SystemExit(0)
            for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
                signal.signal(signum, stop)
            while time.time() < deadline:
                time.sleep(min(1, max(0, deadline - time.time())))
        finally:
            connection_file.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('install')
    approval = commands.add_parser('authorize')
    approval.add_argument('--request', required=True, help='Public HABAB1 request printed on Windows')
    for action in ('revoke', 'serve'):
        command = commands.add_parser(action)
        command.add_argument('--session', required=True)
        if action == 'serve':
            command.add_argument('--deadline', required=True, type=int)
    args = parser.parse_args()
    if args.action == 'install':
        install()
    elif args.action == 'authorize':
        authorize(args.request)
    elif args.action == 'revoke':
        revoke(args.session)
    else:
        serve(args.session, args.deadline)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        # Never print arbitrary request text, keys, environment or command output.
        print('Support relay failed: ' + type(error).__name__, file=sys.stderr)
        raise SystemExit(1) from None
