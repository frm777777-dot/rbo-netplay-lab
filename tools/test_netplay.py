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
    players = 3

    def setUp(self):
        self.room = Room('friends-secret', 'host-secret', frames=120, delay=3, timeout=3, players=self.players)
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

    def ready_room(self):
        clients = [self.connect('host')] + [self.connect() for _ in range(self.players - 1)]
        self.assertTrue(all(client.players == self.players for client in clients))
        with concurrent.futures.ThreadPoolExecutor(self.players) as pool:
            results = list(pool.map(lambda c: c.ready(HEADER), clients))
        self.assertEqual(results, [HEADER] * self.players)
        return clients

    def test_identical_keys_are_isolated_and_delayed_in_order(self):
        # Guests can join first, but can never occupy the host's slot.
        clients = [self.connect(), self.connect('host')] + [self.connect() for _ in range(self.players - 2)]
        self.assertEqual([c.slot for c in clients], [1, 0, 2][:self.players])
        def value(frame, slot):
            phase = frame // 30
            return 0x10 if phase == slot or phase == 3 else 0
        def play(client):
            client.ready(HEADER)
            frames = [client.exchange(value(f, client.slot), None if f == 0 else STATE) for f in range(120)]
            client.finish()
            return frames
        with concurrent.futures.ThreadPoolExecutor(self.players) as pool:
            results = list(pool.map(play, clients))
        self.assertTrue(all(frames == results[0] for frames in results))
        for frame, inputs in enumerate(results[0]):
            expected = [value(frame - 3, slot) if slot < self.players else 0 for slot in range(3)] if frame >= 3 else [0, 0, 0]
            self.assertEqual(inputs, expected)
        self.assertEqual(self.room.completed, set(range(self.players)))
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

    def test_wrong_files_and_excess_client_rejected(self):
        self.connect('host')
        with self.assertRaisesRegex(ProtocolError, 'files differ'):
            self.connect(game='c' * 64)
        for _ in range(self.players - 1):
            self.connect()
        with self.assertRaisesRegex(ProtocolError, 'full'):
            self.connect()

    def test_peer_cannot_supply_another_slot(self):
        clients = self.ready_room()
        send(clients[1].socket, {'type': 'input', 'frame': 0, 'input': 16, 'state': None, 'slot': 0})
        for _ in range(3):
            clients[1].read_frame(_)
        with self.assertRaisesRegex(ProtocolError, 'fields'):
            receive(clients[1].socket)

    def test_out_of_order_frame_stops_room(self):
        client = self.ready_room()[0]
        send(client.socket, {'type': 'input', 'frame': 5, 'input': 16, 'state': STATE})
        for f in range(3):
            client.read_frame(f)
        with self.assertRaisesRegex(ProtocolError, 'order'):
            receive(client.socket)

    def test_out_of_range_input_rejected(self):
        client = self.ready_room()[0]
        send(client.socket, {'type': 'input', 'frame': 0, 'input': 256, 'state': None})
        for f in range(3):
            client.read_frame(f)
        with self.assertRaisesRegex(ProtocolError, 'value'):
            receive(client.socket)

    def test_state_divergence_is_detected(self):
        clients = self.ready_room()
        def play(client):
            try:
                for f in range(10):
                    client.exchange(0, None if f == 0 else ('c' * 64 if client.slot == self.players - 1 else STATE))
            except ProtocolError as exc:
                return str(exc)
        with concurrent.futures.ThreadPoolExecutor(self.players) as pool:
            errors = list(pool.map(play, clients))
        self.assertTrue(all('diverged' in e for e in errors), errors)

    def test_disconnect_stops_other_players(self):
        clients = self.ready_room()
        clients[-1].close()
        def play(client):
            try:
                for f in range(10):
                    client.exchange(0, None if f == 0 else STATE)
            except ProtocolError as exc:
                return str(exc)
        with concurrent.futures.ThreadPoolExecutor(self.players - 1) as pool:
            errors = list(pool.map(play, clients[:-1]))
        self.assertTrue(all(e and 'closed' in e.lower() for e in errors), errors)

    def test_oversized_packet_rejected_before_body_read(self):
        with socket.create_connection(self.server.address) as peer:
            peer.sendall(struct.pack('!I', 2 ** 31))
            with self.assertRaisesRegex(ProtocolError, 'large'):
                receive(peer)

    def test_old_protocol_rejected_without_occupying_slot(self):
        with socket.create_connection(self.server.address) as peer:
            send(peer, {'type': 'hello', 'version': 'rbo-ex3-lab-1',
                        'token': 'friends-secret', 'role': 'guest', 'game_hash': GAME})
            with self.assertRaisesRegex(ProtocolError, 'Protocol mismatch'):
                receive(peer)
        self.assertFalse(self.room.members)
        self.assertIsNone(self.room.error)

    def test_waits_for_exact_room_size(self):
        clients = [self.connect('host')] + [self.connect() for _ in range(self.players - 1)]
        with concurrent.futures.ThreadPoolExecutor(self.players) as pool:
            waiting = [pool.submit(c.ready, HEADER) for c in clients[:-1]]
            # Wait for an observed server state, not an arbitrary sleep.
            with self.room.condition:
                self.assertTrue(self.room.condition.wait_for(lambda: len(self.room.ready) == self.players - 1, timeout=2))
            self.assertTrue(all(not job.done() for job in waiting))
            final = pool.submit(clients[-1].ready, HEADER)
            self.assertEqual([job.result(timeout=2) for job in waiting + [final]], [HEADER] * self.players)


class TwoPlayerNetworkTests(NetworkTests):
    players = 2

    def test_invalid_room_sizes_are_rejected(self):
        for value in (0, 1, 4, True, 2.0, '2'):
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                Room('friends-secret', 'host-secret', players=value)


if __name__ == '__main__':
    unittest.main(verbosity=2)
