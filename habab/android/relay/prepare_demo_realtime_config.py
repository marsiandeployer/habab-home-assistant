"""Create the private config for the isolated public demo Realtime relay."""

import argparse
import json
import os
import re
import stat
from pathlib import Path


SOURCE_CONFIG = Path('/root/.local/share/habab-voice-relay/config.json')
DESTINATION = Path('/root/.local/share/habab-demo-realtime-relay/config-2ca4e031.json')
DEMO_ORIGIN = 'https://habab-demo.wpmix.net'
DEMO_PORT = 18329
DEMO_SESSION_LIMIT = 5
DEMO_SESSIONS_PER_SESSION = 2
DEMO_SESSION_WINDOW_SECONDS = 86400


def build_config(source_path):
    source_path = Path(source_path)
    if source_path.is_symlink() or not source_path.is_file():
        raise ValueError('Existing relay config is not a regular file')
    metadata = source_path.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PermissionError('Existing relay config must be private and owned by this user')
    source = json.loads(source_path.read_text(encoding='utf-8'))
    key_hash = source.get('key_sha256')
    if not isinstance(key_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', key_hash):
        raise ValueError('Existing relay config has no valid service key hash')
    origins = source.get('browser_origins')
    if not isinstance(origins, list) or DEMO_ORIGIN not in origins:
        raise ValueError('Existing relay config does not allow the exact demo origin')
    return {
        'key_sha256': key_hash,
        'port': DEMO_PORT,
        'browser_origins': [DEMO_ORIGIN],
        'demo_openai_session_limit': DEMO_SESSION_LIMIT,
        'demo_openai_sessions_per_session': DEMO_SESSIONS_PER_SESSION,
        'demo_openai_session_window_seconds': DEMO_SESSION_WINDOW_SECONDS,
    }


def write_config(source_path, destination=DESTINATION):
    destination = Path(destination)
    parent = destination.parent
    config = build_config(source_path)
    created_parent = False
    if parent.exists() or parent.is_symlink():
        metadata = parent.stat()
        if (parent.is_symlink() or not parent.is_dir() or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077):
            raise PermissionError('Dedicated demo relay directory must be private and owned by this user')
    else:
        parent.mkdir(mode=0o700, parents=False)
        created_parent = True
    created_file = False
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created_file = True
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(config, output, separators=(',', ':'))
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        try:
            if created_file:
                destination.unlink(missing_ok=True)
            if created_parent:
                parent.rmdir()
        except OSError:
            pass
        raise
    return destination


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-config', type=Path, default=SOURCE_CONFIG)
    parser.add_argument('--destination', type=Path, default=DESTINATION)
    args = parser.parse_args()
    path = write_config(args.source_config, args.destination)
    print(json.dumps({'created': True, 'config_path': str(path), 'port': DEMO_PORT,
                      'browser_origin': DEMO_ORIGIN,
                      'sessions_per_process': DEMO_SESSION_LIMIT,
                      'sessions_per_anonymous_session': DEMO_SESSIONS_PER_SESSION,
                      'session_window_seconds': DEMO_SESSION_WINDOW_SECONDS}))


if __name__ == '__main__':
    main()
