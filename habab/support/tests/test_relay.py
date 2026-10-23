"""Relay request validation and support listener lifecycle checks."""
import base64
from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
import socket
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

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


class RelayListenerTests(unittest.TestCase):
    def setUp(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]

    def tearDown(self):
        self.listener.close()

    def test_detects_real_loopback_listen_socket_from_proc_net_tcp(self):
        self.assertTrue(relay.loopback_listener_present(self.port))
        self.listener.close()
        self.assertFalse(relay.loopback_listener_present(self.port))

    def test_listener_check_fails_closed_on_proc_read_or_parse_errors(self):
        with self.assertRaisesRegex(ValueError, 'Cannot inspect'):
            relay.loopback_listener_present(self.port, '/missing/fake/proc-net-tcp')
        with tempfile.TemporaryDirectory() as directory:
            malformed = Path(directory) / 'tcp'
            malformed.write_text('sl local_address rem_address st\nnot-a-row\n', encoding='ascii')
            with self.assertRaisesRegex(ValueError, 'Cannot parse'):
                relay.loopback_listener_present(self.port, str(malformed))

    def run_serve(self, state_directory, sleep_hook, target_check=None):
        username = 'habab-s-test-session'
        target = SimpleNamespace(pw_uid=12345)
        (state_directory / SESSION).mkdir(parents=True, exist_ok=True)
        if target_check is None:
            target_check = lambda _port: '2026.10.0'
        with patch.object(relay, 'STATE', state_directory), \
                patch.object(relay, 'session_values', return_value=(username, self.port)), \
                patch.object(relay.pwd, 'getpwnam', return_value=target), \
                patch.object(relay.os, 'geteuid', return_value=target.pw_uid), \
                patch.object(relay, 'check_target', target_check), \
                patch.object(relay.signal, 'signal'), \
                patch.object(relay.time, 'sleep', side_effect=sleep_hook), \
                redirect_stdout(io.StringIO()):
            relay.serve(SESSION, int(time.time()) + 20)

    def test_ha_target_can_be_unavailable_while_ssh_listener_remains_live(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            connection_file = state / SESSION / 'connection.json'

            class EndAfterOneCheck(Exception):
                pass

            def stop_after_one_loop(_seconds):
                self.assertTrue(relay.loopback_listener_present(self.port))
                self.assertTrue(connection_file.exists())
                raise EndAfterOneCheck()

            # A live TCP listener with no HA protocol response represents the
            # forwarded port during an HA restart. The one initial target check
            # succeeds; serve must then monitor SSH's listener only.
            target_check = unittest.mock.Mock(return_value='2026.10.0')
            with self.assertRaises(EndAfterOneCheck):
                self.run_serve(state, stop_after_one_loop, target_check)
            target_check.assert_called_once_with(self.port)
            self.assertFalse((state / SESSION / 'connection.json').exists())

    def test_listener_loss_removes_ready_file_and_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            connection_file = state / SESSION / 'connection.json'

            def disconnect_after_ready(_seconds):
                self.assertTrue(connection_file.exists())
                self.listener.close()

            with self.assertRaisesRegex(ValueError, 'listener closed'):
                self.run_serve(state, disconnect_after_ready)
            self.assertFalse(connection_file.exists())


if __name__ == '__main__':
    unittest.main()
