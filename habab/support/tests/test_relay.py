"""Public requests cannot inject SSH options or select arbitrary relay accounts."""
import base64
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('habab_relay', Path(__file__).parents[1] / 'relay.py')
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)
PUBLIC = base64.b64encode(b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20' + b'x' * 32).decode()
SESSION = '1234567890abcdef1234567890abcdef'


class RelayRequestTests(unittest.TestCase):
    def test_public_ed25519_request_has_one_derived_account_and_loopback_port(self):
        self.assertEqual(relay.parse_request(f'HABAB1:{SESSION}:{PUBLIC}:60'), (SESSION, PUBLIC, 60))
        self.assertEqual(relay.session_values(SESSION), ('habab-s-1234567890abcdef', 25660))

    def test_rejects_key_options_commands_private_keys_and_unbounded_sessions(self):
        for value in ('ssh-rsa anything', f'HABAB1:{SESSION}:{PUBLIC}:0',
                      f'HABAB1:{SESSION}:{PUBLIC}:121', f'HABAB1:{SESSION}:{PUBLIC}:60\ncommand=bad',
                      f'HABAB1:../../etc/passwd:{PUBLIC}:60', f'HABAB1:{SESSION}:command=bad:60'):
            with self.subTest(value=value[:20]), self.assertRaises(ValueError):
                relay.parse_request(value)

    def test_rejects_wrong_ssh_wire_key_type_even_when_base64_is_well_formed(self):
        encoded = base64.b64encode(b'not-an-ed25519-key' + b'x' * 34).decode()
        with self.assertRaises(ValueError):
            relay.parse_request(f'HABAB1:{SESSION}:{encoded}:60')

    def test_account_paths_never_come_from_arbitrary_text(self):
        with self.assertRaises(ValueError):
            relay.session_values('root;anything')


if __name__ == '__main__':
    unittest.main()
