"""Socket loss, truncated replies and duplicate delivery must keep one timeline."""
from concurrent.futures import ThreadPoolExecutor
import base64
import json
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import netplay_transport as transport
from netplay_protocol import ProtocolError, RecoveryNeeded, TransportError, SessionEnded, send
from netplay_saves import encode

SAVE = {'format': 1, 'profile': 'a' * 32, 'revision': 4, 'files': {
    f'chr_data{i + 1:02d}.sv4': base64.b64encode(b'\0\0\3\0' + bytes([i + 1]) * 1640).decode()
    for i in range(3)}}
STATE = 'b' * 64


class ResumeTests(unittest.TestCase):
    players = 2
    delay = 0

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.room = transport.RecoveryRoom('guest-secret', 'host-secret', players=self.players, timeout=1,
                                           delay=self.delay, history_frames=24, diagnostics_dir=self.directory.name)
        self.server = transport.RecoveryServer(self.room, ('127.0.0.1', 0))
        self.thread = threading.Thread(target=self.server.serve)
        self.thread.start()
        self.clients = [transport.RecoveryClient(self.server.address,
                        'host-secret' if i == 0 else 'guest-secret', 'a' * 64,
                        'host' if i == 0 else 'guest', timeout=1, status=lambda _: None, resume_seconds=5)
                        for i in range(self.players)]
        self.pool = ThreadPoolExecutor(self.players)
        list(self.pool.map(lambda c: c.ready(encode(SAVE) if c.slot == 0 else None), self.clients))

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.server.close()
        self.thread.join(3)
        self.pool.shutdown(wait=True)
        self.directory.cleanup()

    def exchange(self, checkpoint=None):
        frame = self.clients[0].frame
        self.assertTrue(all(c.frame == frame for c in self.clients))
        values = [(frame + i) % 256 for i in range(self.players)]
        result = list(self.pool.map(lambda c: c.exchange(values[c.slot], STATE if frame else None,
                           checkpoint=checkpoint if c.slot == 0 else None, detail={'scene': 0}), self.clients))
        expected = ([(frame - self.delay + i) % 256 for i in range(self.players)]
                    if frame >= self.delay else [0] * self.players) + [0] * (3 - self.players)
        self.assertEqual(result, [expected] * self.players)

    def test_repeated_socket_loss_keeps_frames_seats_and_save(self):
        newest = dict(SAVE, revision=5)
        tickets = [c.ticket for c in self.clients]
        for frame in range(48):
            if frame in (4, 10, 16, 25, 34):
                client = self.clients[frame % self.players]
                client.socket.shutdown(socket.SHUT_RDWR)
                client.close()
            self.exchange(newest if frame == 8 else None)
        self.assertEqual(self.room.epoch, 0)
        self.assertIsNone(self.room.error)
        self.assertEqual([c.ticket for c in self.clients], tickets)
        self.assertEqual(self.room.checkpoint, newest)
        self.assertTrue(all(c.received_checkpoint == newest for c in self.clients))
        self.assertEqual([c.frame for c in self.clients], [48] * self.players)
        self.assertGreaterEqual(sum(c.transport_resumes for c in self.clients), 5)
        self.assertLessEqual(len(self.room.pending), 25)

    def test_lost_or_truncated_committed_reply_is_replayed_once(self):
        destination = self.clients[-1].socket.getsockname()
        original_send = transport.send
        injected = threading.Event()
        def drop_reply(connection, packet):
            if (packet.get('type') == 'frame' and packet['frame'] == 4 + self.delay
                    and connection.getpeername() == destination and not injected.is_set()):
                injected.set()
                encoded = encode(packet)
                # Receiver obtains a valid length and an incomplete JSON body.
                connection.sendall(struct.pack('!I', len(encoded)) + encoded[:9])
                connection.shutdown(socket.SHUT_RDWR)
                raise TransportError('Injected lost response after commit')
            return original_send(connection, packet)
        with patch.object(transport, 'send', side_effect=drop_reply):
            for _ in range(30):
                self.exchange()
        self.assertTrue(injected.is_set())
        self.assertEqual(self.room.epoch, 0)
        self.assertIsNone(self.room.error)
        frames = [item['frame'] for item in self.room.audit]
        self.assertEqual(frames, list(range(len(frames))))
        self.assertEqual(self.room.audit_count, len(frames))

    def test_conflicting_duplicate_is_rejected(self):
        for _ in range(self.delay + 3):
            self.exchange()
        packet = {'type': 'input', 'epoch': 0, 'frame': 0, 'input': 99, 'state': None, 'detail': {'scene': 0}}
        with self.assertRaisesRegex(ProtocolError, 'Conflicting'):
            self.room.submit_generation(0, self.room.leases[0], 0, packet)
        self.assertEqual(self.room.checkpoint, SAVE)

    def test_stale_connection_cannot_change_new_lease(self):
        for _ in range(self.delay + 2):
            self.exchange()
        old = self.room.leases[0]
        client = self.clients[0]
        client.close()
        self.exchange()
        with self.assertRaisesRegex(ProtocolError, 'Superseded'):
            self.room.check_lease(0, old, 0)
        self.assertEqual(self.room.epoch, 0)
        self.exchange()

    def test_real_state_mismatch_is_not_hidden_by_transport_resume(self):
        self.exchange()
        future = dict(SAVE, revision=5)
        def diverge(c):
            try:
                c.exchange(16, 'c' * 64 if c.slot else STATE,
                           checkpoint=future if c.slot == 0 else None, detail={'scene': 0})
                for _ in range(self.delay + 1):
                    c.exchange(0, STATE, detail={'scene': 0})
            except RecoveryNeeded:
                return True
            return False
        self.assertEqual(list(self.pool.map(diverge, self.clients)), [True] * self.players)
        self.assertEqual(self.room.epoch, 1)
        self.assertEqual(self.room.checkpoint, SAVE)
        reports = list(Path(self.directory.name).glob('*-state_mismatch.json'))
        self.assertEqual(len(reports), 1)
        report = json.loads(reports[0].read_bytes())
        self.assertEqual(len(report['players']), self.players)
        list(self.pool.map(lambda c: c.reconnect(encode(SAVE) if c.slot == 0 else None,
                                               status=lambda _: None, deadline_seconds=5), self.clients))
        self.exchange()

    def test_manual_quit_still_ends_room(self):
        for _ in range(self.delay + 2):
            self.exchange()
        send(self.clients[0].socket, {'type': 'stop'})
        def notice(c):
            try:
                for _ in range(self.delay + 3):
                    c.exchange(0, STATE)
            except SessionEnded:
                return True
            return False
        self.assertEqual(list(self.pool.map(notice, self.clients)), [True] * self.players)
        self.assertEqual(self.room.checkpoint, SAVE)
        self.assertEqual(self.room.epoch, 0)

    def test_save_payload_is_sent_only_on_confirmed_changes(self):
        for _ in range(self.delay + 3):
            self.exchange()
        self.assertTrue(all('checkpoint' not in e['reply'] for e in self.room.pending.values() if e['reply']))
        self.assertTrue(all(c.received_checkpoint == SAVE for c in self.clients))

    def test_quit_after_losing_socket_does_not_restart_game(self):
        for _ in range(self.delay + 2):
            self.exchange()
        self.clients[0].close()
        self.clients[0].finish()
        self.assertIsNotNone(self.room.stop_reason)
        self.assertEqual(self.room.epoch, 0)
        self.assertEqual(self.room.checkpoint, SAVE)

    def test_both_diagnostic_snapshots_reach_host_without_changing_lease(self):
        leases = dict(self.room.leases)
        for c in self.clients:
            self.assertTrue(c.report_failure({'frame': 7, 'state': {'slot': c.slot}, 'entities': [], 'recent': []}))
        reports = list(Path(self.directory.name).glob('*-peer_diagnostic.json'))
        self.assertEqual(len(reports), self.players)
        self.assertEqual(self.room.leases, leases)
        self.assertIsNone(self.room.error)
        self.exchange()

    def test_unauthenticated_control_cannot_close_room(self):
        c = self.clients[0]
        with patch.object(c, 'token', 'wrong'):
            with self.assertRaises(ProtocolError):
                c.control('stop')
        self.assertIsNone(self.room.stop_reason)
        self.assertIsNone(self.room.error)
        self.exchange()

    def test_damaged_diagnostic_cannot_close_room(self):
        with self.assertRaises(ProtocolError):
            self.clients[0].control('diagnostic', 'not-zlib')
        self.assertIsNone(self.room.error)
        self.exchange()

    def test_half_open_old_sockets_do_not_exhaust_server_connections(self):
        for _ in range(self.delay + 2):
            self.exchange()
        abandoned = []
        try:
            for _ in range(12):
                c = self.clients[-1]
                abandoned.append(c.socket)  # No FIN from the old connection.
                c.connect(live=True)
                for frame in sorted(c.pending):
                    send(c.socket, c.pending[frame])
                self.exchange()
            self.assertEqual(self.room.epoch, 0)
            self.assertIsNone(self.room.error)
            self.assertLessEqual(len(self.server.connections), self.players + 1)
        finally:
            for connection in abandoned:
                connection.close()


class ThreePlayerPipelineTests(ResumeTests):
    players = 3
    delay = 3


class MaximumPipelineTests(ResumeTests):
    delay = 12


class OutageTests(ResumeTests):
    # Run slow outage cases only once, with a pipelined two-player room.
    delay = 3

    def test_outage_longer_than_socket_timeout_retains_timeline(self):
        for _ in range(8):
            self.exchange()
        client = self.clients[1]
        client.close()
        original = client.connect
        resumes_after = time.monotonic() + 2.1
        def unavailable(*args, **kwargs):
            if time.monotonic() < resumes_after:
                raise OSError('Injected internet outage')
            return original(*args, **kwargs)
        with patch.object(client, 'connect', side_effect=unavailable):
            for _ in range(12):
                self.exchange()
        self.assertEqual(self.room.epoch, 0)
        self.assertEqual(self.room.checkpoint, SAVE)
        self.assertEqual(self.clients[0].frame, 20)

    def test_exhausted_outage_does_not_replace_saves(self):
        for _ in range(8):
            self.exchange()
        client = self.clients[1]
        client.close()
        client.resume_seconds = .01
        with patch.object(client, 'connect', side_effect=OSError('Injected unreachable server')):
            with self.assertRaisesRegex(ProtocolError, '초과'):
                client.exchange(0, STATE)
        self.assertEqual(self.room.checkpoint, SAVE)
        self.assertEqual(self.room.epoch, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
