"""Socket integration tests for slot isolation, frame barriers and disconnects."""
import concurrent.futures
import json
from pathlib import Path
import socket
import struct
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from netplay_protocol import Client, ProtocolError, Room, Server, VERSION, receive, send

GAME = 'a' * 64
STATE = 'b' * 64
HEADER = b'\0' * 0x1344


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.room = Room('friends-secret', 'host-secret', frames=120, delay=3, timeout=3)
        self.server = Server(self.room, ('127.0.0.1', 0))
        self.thread = threading.Thread(target=self.server.serve)
        self.thread.start()
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.server.close()
        self.thread.join(2)

    def connect(self, role='guest', token=None, game=GAME):
        client = Client(self.server.address, token or ('host-secret' if role == 'host' else 'friends-secret'), game, role, timeout=4)
        self.clients.append(client)
        return client

    def ready_three(self):
        clients = [self.connect('host'), self.connect(), self.connect()]
        with concurrent.futures.ThreadPoolExecutor(3) as pool:
            results = list(pool.map(lambda c: c.ready(HEADER), clients))
        self.assertEqual(results, [HEADER] * 3)
        return clients

    def test_identical_keys_are_isolated_and_delayed_in_order(self):
        # Guests can join first, but can never occupy the host's slot.
        clients = [self.connect(), self.connect('host'), self.connect()]
        self.assertEqual([c.slot for c in clients], [1, 0, 2])
        def value(frame, slot):
            phase = frame // 30
            return 0x10 if phase == slot or phase == 3 else 0
        def play(client):
            client.ready(HEADER)
            frames = [client.exchange(value(f, client.slot), None if f == 0 else STATE) for f in range(120)]
            client.finish()
            return frames
        with concurrent.futures.ThreadPoolExecutor(3) as pool:
            results = list(pool.map(play, clients))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])
        for frame, inputs in enumerate(results[0]):
            expected = [value(frame - 3, slot) for slot in range(3)] if frame >= 3 else [0, 0, 0]
            self.assertEqual(inputs, expected)
        self.assertEqual(self.room.completed, {0, 1, 2})
        self.assertFalse(self.room.pending)
        self.assertIsNone(self.room.error)

    def test_wrong_token_rejected_without_stopping_room(self):
        with self.assertRaisesRegex(ProtocolError, 'token'):
            self.connect(token='wrong')
        self.assertIsNone(self.room.error)
        self.assertEqual(self.connect('host').slot, 0)

    def test_guest_cannot_claim_host(self):
        with self.assertRaisesRegex(ProtocolError, 'token'):
            self.connect('host', token='friends-secret')

    def test_wrong_files_and_fourth_client_rejected(self):
        self.connect('host')
        with self.assertRaisesRegex(ProtocolError, 'files differ'):
            self.connect(game='c' * 64)
        self.connect()
        self.connect()
        with self.assertRaisesRegex(ProtocolError, 'full'):
            self.connect()

    def test_peer_cannot_supply_another_slot(self):
        clients = self.ready_three()
        send(clients[1].socket, {'type': 'input', 'frame': 0, 'input': 16, 'state': None, 'slot': 0})
        for _ in range(3):
            clients[1].read_frame(_)
        with self.assertRaisesRegex(ProtocolError, 'fields'):
            receive(clients[1].socket)

    def test_out_of_order_frame_stops_room(self):
        client = self.ready_three()[0]
        send(client.socket, {'type': 'input', 'frame': 5, 'input': 16, 'state': STATE})
        for f in range(3):
            client.read_frame(f)
        with self.assertRaisesRegex(ProtocolError, 'order'):
            receive(client.socket)

    def test_out_of_range_input_rejected(self):
        client = self.ready_three()[0]
        send(client.socket, {'type': 'input', 'frame': 0, 'input': 256, 'state': None})
        for f in range(3):
            client.read_frame(f)
        with self.assertRaisesRegex(ProtocolError, 'value'):
            receive(client.socket)

    def test_state_divergence_is_detected(self):
        clients = self.ready_three()
        def play(client):
            try:
                for f in range(10):
                    client.exchange(0, None if f == 0 else ('c' * 64 if client.slot == 2 else STATE))
            except ProtocolError as exc:
                return str(exc)
        with concurrent.futures.ThreadPoolExecutor(3) as pool:
            errors = list(pool.map(play, clients))
        self.assertTrue(all('diverged' in e for e in errors), errors)

    def test_disconnect_stops_other_players(self):
        clients = self.ready_three()
        clients[2].close()
        def play(client):
            try:
                for f in range(10):
                    client.exchange(0, None if f == 0 else STATE)
            except ProtocolError as exc:
                return str(exc)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            errors = list(pool.map(play, clients[:2]))
        self.assertTrue(all(e and 'closed' in e.lower() for e in errors), errors)

    def test_oversized_packet_rejected_before_body_read(self):
        with socket.create_connection(self.server.address) as peer:
            peer.sendall(struct.pack('!I', 2 ** 31))
            with self.assertRaisesRegex(ProtocolError, 'large'):
                receive(peer)


if __name__ == '__main__':
    unittest.main(verbosity=2)
