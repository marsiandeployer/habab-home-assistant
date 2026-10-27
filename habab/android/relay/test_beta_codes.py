"""Beta credentials are per install, stable, bounded and independently stoppable."""

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import uuid

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from beta_codes import BetaCodes, create_config


class BetaCodesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'private' / 'beta.json'
        create_config(self.path)
        self.codes = BetaCodes(self.path)
        self.client = await self.make_client(self.codes)
        self.installation_id = str(uuid.uuid4())

    async def make_client(self, codes):
        app = web.Application()
        codes.register_routes(app)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def issue(self, installation_id=None, client=None):
        return await (client or self.client).post('/beta/enroll', json={
            'installation_id': installation_id or self.installation_id,
        })

    def set_config(self, **changes):
        config = json.loads(self.path.read_text())
        config.update(changes)
        self.path.write_text(json.dumps(config))

    async def test_idempotent_per_install_and_stable_across_restart(self):
        response = await self.issue()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        result = await response.json()
        self.assertEqual(result['installation_id'], self.installation_id)
        self.assertTrue(self.codes.verify(result['code']))
        self.assertTrue(BetaCodes(self.path).verify(result['code']))
        self.assertEqual((await (await self.issue()).json())['code'], result['code'])
        other = await (await self.issue(str(uuid.uuid4()))).json()
        self.assertNotEqual(other['code'], result['code'])

    async def test_tampered_and_malformed_codes_are_rejected(self):
        token = (await (await self.issue()).json())['code']
        other_install = uuid.uuid4().hex
        invalid = [None, 12, b'code', '', token.upper(), token + '=', token + '\n',
                   token.replace('hb1.', 'hb2.'), token[:-1], 'x' * 10000,
                   'hb1.' + other_install + '.' + token.split('.')[2],
                   token[:37] + ('A' if token[37] != 'A' else 'B') + token[38:],
                   'hb1.' + '0' * 32 + '.' + token.split('.')[2],
                   token[:37] + '\N{LATIN CAPITAL LETTER A WITH RING ABOVE}' + token[38:]]
        for candidate in invalid:
            with self.subTest(candidate_type=type(candidate).__name__):
                self.assertFalse(self.codes.verify(candidate))

    async def test_stopping_issuance_keeps_existing_tokens_valid_without_disk_read(self):
        token = (await (await self.issue()).json())['code']
        self.set_config(issue_codes_enabled=False)
        response = await self.issue(str(uuid.uuid4()))
        self.assertEqual(response.status, 403)
        self.assertEqual((await response.json())['error']['code'], 'habab_beta_issuance_disabled')
        with patch('beta_codes._read_config', side_effect=AssertionError('verification touched disk')):
            self.assertTrue(self.codes.verify(token))
        self.assertTrue(BetaCodes(self.path).verify(token))
        self.set_config(issue_codes_enabled=True)
        self.assertEqual((await self.issue()).status, 200)

    async def test_absent_configuration_neither_issues_nor_authorizes(self):
        token = (await (await self.issue()).json())['code']
        for codes in (BetaCodes(), BetaCodes(self.path.parent / 'missing.json')):
            self.assertFalse(codes.verify(token))
            response = await self.issue(client=await self.make_client(codes))
            self.assertEqual(response.status, 503)

    async def test_invalid_request_format_and_body_limits(self):
        malformed = [None, [], {}, {'installation_id': self.installation_id, 'extra': 1},
                     {'installation_id': None}, {'installation_id': 123},
                     {'installation_id': self.installation_id.replace('-', '')},
                     {'installation_id': self.installation_id.upper()},
                     {'installation_id': str(uuid.uuid1())},
                     {'installation_id': '00000000-0000-0000-0000-000000000000'}]
        for payload in malformed:
            response = await self.client.post('/beta/enroll', data=json.dumps(payload),
                                              headers={'Content-Type': 'application/json'})
            self.assertEqual(response.status, 400)
        for body in (b'{', b'\xff'):
            response = await self.client.post('/beta/enroll', data=body,
                                              headers={'Content-Type': 'application/json'})
            self.assertEqual(response.status, 400)
        self.assertEqual((await self.client.post('/beta/enroll', data='{}')).status, 400)
        response = await self.client.post('/beta/enroll', data=b' ' * 1025,
                                          headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status, 413)
        self.assertEqual((await self.client.get('/beta/enroll')).status, 405)

    async def test_corrupt_removed_or_changed_config_fails_closed_without_revocation(self):
        token = (await (await self.issue()).json())['code']
        self.set_config(signing_secret='ab' * 32)
        self.assertEqual((await self.issue()).status, 503)
        self.assertTrue(self.codes.verify(token))
        self.path.write_text('{private-broken-value')
        response = await self.issue()
        self.assertEqual(response.status, 503)
        self.assertNotIn('private-broken-value', await response.text())
        self.assertTrue(self.codes.verify(token))
        self.path.unlink()
        self.assertEqual((await self.issue()).status, 503)
        self.assertTrue(self.codes.verify(token))

    async def test_ip_attempt_limit_and_expiry(self):
        self.codes.ISSUE_PER_IP = 2
        with patch.object(self.codes, '_clock', return_value=100):
            self.assertEqual((await self.issue()).status, 200)
            self.assertEqual((await self.issue()).status, 200)
            response = await self.issue()
            self.assertEqual(response.status, 429)
            self.assertEqual((await response.json())['error']['code'], 'habab_beta_rate_limited')
        with patch.object(self.codes, '_clock', return_value=161):
            self.assertEqual((await self.issue()).status, 200)

    async def test_global_limit_bounds_ip_memory_and_ignores_forwarded_headers(self):
        self.codes.ISSUE_GLOBAL = 3
        with patch.object(self.codes, '_clock', return_value=100):
            for index in range(3):
                self.assertTrue(self.codes._admit(str(index)))
            for index in range(1000):
                self.assertFalse(self.codes._admit('extra-' + str(index)))
            self.assertEqual(len(self.codes._attempts_by_ip), 3)
        with patch.object(self.codes, '_clock', return_value=161):
            self.assertTrue(self.codes._admit('fresh'))
            self.assertEqual(set(self.codes._attempts_by_ip), {'fresh'})
        self.codes.ISSUE_PER_IP = 1
        self.assertEqual((await self.issue()).status, 200)
        response = await self.client.post('/beta/enroll', json={'installation_id': self.installation_id},
                                          headers={'X-Forwarded-For': '192.0.2.1'})
        self.assertEqual(response.status, 429)


class BetaConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'private' / 'beta.json'

    def test_creation_is_private_and_never_overwrites(self):
        self.assertEqual(create_config(self.path), self.path)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        before = self.path.read_bytes()
        with self.assertRaises(FileExistsError):
            create_config(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        config = json.loads(before)
        self.assertEqual(len(bytes.fromhex(config['signing_secret'])), 32)
        self.assertIs(config['issue_codes_enabled'], True)

    def test_unsafe_modes_and_symlinks_are_rejected(self):
        create_config(self.path)
        self.path.chmod(0o644)
        with self.assertRaises(PermissionError):
            BetaCodes(self.path)
        self.path.chmod(0o600)
        self.path.parent.chmod(0o755)
        with self.assertRaises(PermissionError):
            BetaCodes(self.path)
        with self.assertRaises(PermissionError):
            create_config(self.path.parent / 'other.json')
        self.path.parent.chmod(0o700)
        linked_file = self.path.parent / 'linked.json'
        linked_file.symlink_to(self.path)
        with self.assertRaises(OSError):
            BetaCodes(linked_file)
        linked_directory = Path(self.directory.name) / 'linked'
        linked_directory.symlink_to(self.path.parent, target_is_directory=True)
        with self.assertRaises(OSError):
            BetaCodes(linked_directory / 'beta.json')

    def test_owner_and_config_validation_are_enforced(self):
        create_config(self.path)
        with patch('beta_codes.os.getuid', return_value=os.getuid() + 1):
            with self.assertRaises(PermissionError):
                BetaCodes(self.path)
        for config in ({}, [], {'signing_secret': 'a' * 64, 'issue_codes_enabled': 1},
                       {'signing_secret': 'a' * 63, 'issue_codes_enabled': True},
                       {'signing_secret': 'Z' * 64, 'issue_codes_enabled': True}):
            self.path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, '^Invalid beta config$'):
                BetaCodes(self.path)
        self.path.write_text('x' * 4097)
        with self.assertRaisesRegex(ValueError, '^Invalid beta config$'):
            BetaCodes(self.path)


if __name__ == '__main__':
    unittest.main()
