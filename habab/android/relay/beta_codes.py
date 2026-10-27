"""Stateless per-install free-beta credentials; no upstream keys or registry.

The private config contains a stable 32-byte hex ``signing_secret`` and a
boolean ``issue_codes_enabled``. Only the latter is reloaded for enrollment.
Stopping issuance does not revoke credentials already held by installations.
"""

import base64
from collections import deque
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
import uuid

from aiohttp import web


_TOKEN = re.compile(r'hb1\.([0-9a-f]{32})\.([A-Za-z0-9_-]{43})', re.ASCII)
_HEX_SECRET = re.compile(r'[0-9a-f]{64}', re.ASCII)
_CONFIG_LIMIT = 4096
_BODY_LIMIT = 1024


def _private_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    metadata = os.fstat(descriptor)
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        os.close(descriptor)
        raise PermissionError('Beta config directory must be owned by this user with mode 0700')
    return descriptor


def _read_config(path):
    directory = _private_directory(path.parent)
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        with os.fdopen(descriptor, 'rb') as source:
            metadata = os.fstat(source.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600):
                raise PermissionError('Beta config must be a regular owned file with mode 0600')
            raw = source.read(_CONFIG_LIMIT + 1)
    finally:
        os.close(directory)
    try:
        config = json.loads(raw) if len(raw) <= _CONFIG_LIMIT else None
        if (not isinstance(config, dict)
                or not isinstance(config.get('signing_secret'), str)
                or not _HEX_SECRET.fullmatch(config['signing_secret'])
                or type(config.get('issue_codes_enabled')) is not bool):
            raise ValueError
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('Invalid beta config') from None
    return config


def create_config(config_path, *, issue_codes_enabled=True):
    """Create once, never overwrite or rotate a signing secret; return its path."""
    if type(issue_codes_enabled) is not bool:
        raise ValueError('issue_codes_enabled must be boolean')
    path = Path(config_path)
    try:
        path.parent.mkdir(mode=0o700)
    except FileExistsError:
        pass
    directory = _private_directory(path.parent)
    try:
        descriptor = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as destination:
                os.fchmod(destination.fileno(), 0o600)
                json.dump({'signing_secret': secrets.token_hex(32),
                           'issue_codes_enabled': issue_codes_enabled}, destination)
                destination.write('\n')
                destination.flush()
                os.fsync(destination.fileno())
        except BaseException:
            os.unlink(path.name, dir_fd=directory)
            raise
    finally:
        os.close(directory)
    return path


def _installation_id(value):
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    if str(parsed) != value or parsed.version != 4 or parsed.variant != uuid.RFC_4122:
        return None
    return parsed


class BetaCodes:
    """Load signing material once; bound public enrollment attempts in RAM."""

    ISSUE_PER_IP = 20
    ISSUE_GLOBAL = 120
    ISSUE_WINDOW_SECONDS = 60
    _clock = staticmethod(time.monotonic)

    def __init__(self, config_path=None):
        self.config_path = Path(config_path) if config_path else None
        self._secret = None
        self._attempts = deque()
        self._attempts_by_ip = {}
        if self.config_path is not None:
            try:
                config = _read_config(self.config_path)
            except FileNotFoundError:
                return
            self._secret = bytes.fromhex(config['signing_secret'])

    def _signature(self, installation_hex):
        digest = hmac.new(self._secret, b'habab-beta-v1:' + installation_hex.encode('ascii'),
                          hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')

    def verify(self, token):
        """Validate an issued token without filesystem access or issuer checks."""
        if self._secret is None or not isinstance(token, str) or len(token) != 80:
            return False
        match = _TOKEN.fullmatch(token)
        if match is None:
            return False
        installation_hex, signature = match.groups()
        installation = uuid.UUID(hex=installation_hex)
        if installation.version != 4 or installation.variant != uuid.RFC_4122:
            return False
        return hmac.compare_digest(signature, self._signature(installation_hex))

    def _admit(self, ip):
        now = self._clock()
        cutoff = now - self.ISSUE_WINDOW_SECONDS
        while self._attempts and self._attempts[0] <= cutoff:
            self._attempts.popleft()
        for address, attempts in tuple(self._attempts_by_ip.items()):
            while attempts and attempts[0] <= cutoff:
                attempts.popleft()
            if not attempts:
                del self._attempts_by_ip[address]
        if len(self._attempts) >= self.ISSUE_GLOBAL:
            return False
        attempts = self._attempts_by_ip.get(ip)
        if attempts is not None and len(attempts) >= self.ISSUE_PER_IP:
            return False
        self._attempts.append(now)
        self._attempts_by_ip.setdefault(ip, deque()).append(now)
        return True

    @staticmethod
    def _error(code, status):
        return web.json_response({'error': {'code': code}}, status=status,
                                 headers={'Cache-Control': 'no-store'})

    async def enroll(self, request):
        if not self._admit(request.remote or 'unknown'):
            return self._error('habab_beta_rate_limited', 429)
        if self._secret is None:
            return self._error('habab_beta_unavailable', 503)
        try:
            config = _read_config(self.config_path)
        except (OSError, ValueError):
            return self._error('habab_beta_unavailable', 503)
        if not hmac.compare_digest(bytes.fromhex(config['signing_secret']), self._secret):
            return self._error('habab_beta_unavailable', 503)
        if not config['issue_codes_enabled']:
            return self._error('habab_beta_issuance_disabled', 403)
        if request.content_type != 'application/json':
            return self._error('habab_beta_invalid_request', 400)
        body = bytearray()
        async for chunk in request.content.iter_chunked(_BODY_LIMIT + 1):
            body.extend(chunk)
            if len(body) > _BODY_LIMIT:
                return self._error('habab_beta_invalid_request', 413)
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            return self._error('habab_beta_invalid_request', 400)
        installation = (_installation_id(payload.get('installation_id'))
                        if isinstance(payload, dict) and set(payload) == {'installation_id'} else None)
        if installation is None:
            return self._error('habab_beta_invalid_request', 400)
        code = 'hb1.' + installation.hex + '.' + self._signature(installation.hex)
        return web.json_response({'installation_id': str(installation), 'code': code},
                                 headers={'Cache-Control': 'no-store'})

    def register_routes(self, app):
        app.router.add_post('/beta/enroll', self.enroll)
