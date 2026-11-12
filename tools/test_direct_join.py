"""Direct IP/port joins, preference editing and private reconnect identities."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import netplay_launcher as launcher
from netplay_protocol import ProtocolError
from netplay_saves import encode

GAME = 'a' * 64
SAVE = {'format': 1, 'profile': 'b' * 32, 'revision': 0, 'files': {}}


class JoinPromptTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = Path(directory.name) / 'join-settings.json'
        self.redirect = contextlib.redirect_stdout(io.StringIO())
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def test_first_join_asks_only_address_and_port_then_remembers_them(self):
        with patch('builtins.input', side_effect=['192.0.2.10', '']) as prompt, \
             patch.object(launcher, 'Client') as client:
            self.assertIs(launcher.connect_guest(GAME, self.settings), client.return_value)
        self.assertEqual(prompt.call_count, 2)
        client.assert_called_once_with(('192.0.2.10', 47624), launcher.DIRECT_JOIN_TOKEN, GAME,
                                       timeout=45, status=launcher.connection_status)
        self.assertEqual(json.loads(self.settings.read_bytes()), {'address': '192.0.2.10', 'port': 47624})

    def test_remembered_endpoint_needs_only_two_enters(self):
        launcher.save_join_settings(self.settings, '192.0.2.10', 47625)
        with patch('builtins.input', side_effect=['', '']) as prompt, patch.object(launcher, 'Client') as client:
            launcher.connect_guest(GAME, self.settings)
        self.assertEqual(prompt.call_count, 2)
        self.assertEqual(client.call_args.args[0], ('192.0.2.10', 47625))

    def test_explicit_endpoint_does_not_read_stdin(self):
        with patch('builtins.input', side_effect=AssertionError('Unexpected room-code prompt')), \
             patch.object(launcher, 'Client'):
            launcher.connect_guest(GAME, self.settings, '192.0.2.10', 47624)

    def test_failed_join_retries_without_requesting_a_code(self):
        connected = object()
        with patch('builtins.input', side_effect=['']) as prompt, \
             patch.object(launcher, 'Client', side_effect=[TimeoutError('test timeout'), connected]) as client:
            self.assertIs(launcher.connect_guest(GAME, self.settings, '192.0.2.10', 47624), connected)
        self.assertEqual(prompt.call_count, 1)
        self.assertEqual(client.call_args_list[0], client.call_args_list[1])

    def test_failed_join_can_change_endpoint_and_save_it(self):
        with patch('builtins.input', side_effect=['2', '192.0.2.20', '47626']) as prompt, \
             patch.object(launcher, 'Client', side_effect=[TimeoutError('test timeout'), object()]) as client:
            launcher.connect_guest(GAME, self.settings, '192.0.2.10', 47624)
        self.assertEqual(prompt.call_count, 3)
        self.assertEqual(client.call_args.args[0], ('192.0.2.20', 47626))
        self.assertEqual(launcher.read_join_settings(self.settings), ('192.0.2.20', 47626))


class DirectSocketTests(unittest.TestCase):
    def test_two_and_three_player_join_resume_and_host_identity(self):
        for players in (2, 3):
            with self.subTest(players=players), tempfile.TemporaryDirectory() as directory:
                room = launcher.Room(launcher.DIRECT_JOIN_TOKEN, 'private-host-test-key',
                                     players=players, delay=0, timeout=2)
                server = launcher.Server(room, ('127.0.0.1', 0))
                thread = threading.Thread(target=server.serve)
                thread.start()
                clients = []
                try:
                    # The public join marker must not grant the host seat.
                    with self.assertRaisesRegex(ProtocolError, 'token'):
                        launcher.Client(server.address, launcher.DIRECT_JOIN_TOKEN, GAME, 'host', timeout=2)
                    self.assertIsNone(room.error)
                    clients.append(launcher.Client(server.address, room.host_token, GAME, 'host',
                                                   timeout=2, status=lambda _: None))
                    for index in range(1, players):
                        with patch('builtins.input', side_effect=AssertionError('Unexpected prompt')), \
                             contextlib.redirect_stdout(io.StringIO()):
                            clients.append(launcher.connect_guest(GAME, Path(directory) / f'join-{index}.json',
                                                                  *server.address))
                        clients[-1].status = lambda _: None
                    with ThreadPoolExecutor(players) as pool:
                        headers = list(pool.map(lambda c: c.ready(encode(SAVE) if c.slot == 0 else None), clients))
                        self.assertEqual(headers, [encode(SAVE)] * players)
                        tickets = [c.ticket for c in clients]
                        for frame in range(8):
                            if frame == 3:
                                clients[-1].socket.shutdown(socket.SHUT_RDWR)
                                clients[-1].close()
                            replies = list(pool.map(lambda c: c.exchange(16 << c.slot,
                                                None if frame == 0 else 'c' * 64), clients))
                            expected = [16 << slot for slot in range(players)] + [0] * (3 - players)
                            self.assertEqual(replies, [expected] * players)
                        self.assertEqual([c.slot for c in clients], list(range(players)))
                        self.assertEqual([c.ticket for c in clients], tickets)
                        self.assertEqual(len(set(tickets)), players)
                        self.assertGreaterEqual(clients[-1].transport_resumes, 1)
                        self.assertEqual(room.epoch, 0)
                        self.assertEqual(room.checkpoint, SAVE)
                        self.assertIsNone(room.error)
                finally:
                    for client in clients:
                        client.close()
                    server.close()
                    thread.join(3)


if __name__ == '__main__':
    unittest.main()
