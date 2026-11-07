"""Real socket fault tests: reconnect, old leases, desync and save retention."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import socket
import struct
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from netplay_protocol import ProtocolError, RecoveryNeeded, SessionEnded, send
from netplay_recovery import RecoveryRoom, RecoveryServer, RecoveryClient
from netplay_saves import encode

SAVE = {'format': 1, 'profile': 'a' * 32, 'revision': 4, 'files': {
    f'chr_data{i + 1:02d}.sv4': base64.b64encode(b'\0\0\3\0' + bytes([i + 1]) * 1640).decode()
    for i in range(3)}}
STATE = 'b' * 64


def stage(players):
    blob = bytearray(0x660 * 3)
    for i in range(3):
        struct.pack_into('<i', blob, 0x660 * i, i + 2 if i < players else -1)
    return {'format': 1, 'stage': 3, 'characters': base64.b64encode(blob).decode(),
            'slots': [0, 1, 2], 'rng': [123, 456, 789]}


class RecoveryTests(unittest.TestCase):
    players = 3

    def setUp(self):
        self.room = RecoveryRoom('guest-secret', 'host-secret', players=self.players, timeout=3, delay=0)
        self.server = RecoveryServer(self.room, ('127.0.0.1', 0))
        self.thread = threading.Thread(target=self.server.serve)
        self.thread.start()
        self.clients = [RecoveryClient(self.server.address, 'host-secret' if i == 0 else 'guest-secret', 'a' * 64,
                                       'host' if i == 0 else 'guest', timeout=4) for i in range(self.players)]
        self.pool = ThreadPoolExecutor(self.players)
        self.assertEqual(list(self.pool.map(lambda c: c.ready(encode(SAVE) if c.slot == 0 else None), self.clients)),
                         [encode(SAVE)] * self.players)

    def tearDown(self):
        for c in self.clients:
            c.close()
        self.server.close()
        self.thread.join(3)
        self.pool.shutdown(wait=True)

    def exchange(self, state=STATE, checkpoint=None, snapshot=None):
        detail = {'scene': 0}
        if snapshot is not None:
            detail['stage_checkpoint'] = hashlib.sha256(encode(snapshot)).hexdigest()
        return list(self.pool.map(lambda c: c.exchange(16, state if c.frame else None,
                         checkpoint=checkpoint if c.slot == 0 else None, detail=detail,
                         stage=snapshot if c.slot == 0 else None), self.clients))

    def reconnect_all(self):
        tickets = [c.ticket for c in self.clients]
        results = list(self.pool.map(lambda c: c.reconnect(encode(SAVE) if c.slot == 0 else None,
                                                         status=lambda _: None, deadline_seconds=10), self.clients))
        self.assertEqual([c.ticket for c in self.clients], tickets)
        self.assertEqual([c.slot for c in self.clients], list(range(self.players)))
        self.assertEqual(results, [encode(self.room.checkpoint)] * self.players)

    def test_desync_discards_unconfirmed_save_and_restores_stage(self):
        self.exchange()
        snapshot = stage(self.players)
        self.exchange(snapshot=snapshot)
        bad = dict(SAVE, revision=5)
        def diverge(c):
            with self.assertRaises(RecoveryNeeded):
                c.exchange(16, ('c' * 64 if c.slot else STATE), checkpoint=bad if c.slot == 0 else None)
        list(self.pool.map(diverge, self.clients))
        self.assertIsNone(self.room.error)
        self.assertEqual(self.room.checkpoint, SAVE)
        self.reconnect_all()
        self.assertEqual([c.stage for c in self.clients], [snapshot] * self.players)
        self.assertEqual(self.exchange(), [[16] * self.players + [0] * (3 - self.players)] * self.players)

    def test_transport_disconnect_resumes_reserved_seat_and_latest_save(self):
        self.exchange()
        newest = dict(SAVE, revision=5)
        self.exchange(checkpoint=newest, snapshot=stage(self.players))
        lost = self.clients[-1]
        lost.socket.shutdown(socket.SHUT_RDWR)
        lost.close()
        def notice(c):
            with self.assertRaises(RecoveryNeeded):
                c.exchange(16, STATE)
        list(self.pool.map(notice, self.clients[:-1]))
        with self.assertRaisesRegex(ProtocolError, 'full'):
            RecoveryClient(self.server.address, 'guest-secret', 'a' * 64)
        self.reconnect_all()
        self.assertEqual(self.room.checkpoint, newest)
        self.assertEqual([c.received_checkpoint for c in self.clients], [newest] * self.players)
        self.exchange()

    def test_native_crash_request_restarts_everyone(self):
        self.exchange()
        self.exchange(snapshot=stage(self.players))
        self.clients[0].request_recovery()
        def notice(c):
            with self.assertRaises(RecoveryNeeded):
                c.exchange(0, STATE)
        list(self.pool.map(notice, self.clients))
        self.reconnect_all()
        self.assertEqual(self.room.epoch, 1)
        self.exchange()

    def test_repeated_failures_fall_back_to_title_with_all_saves(self):
        self.exchange()
        self.exchange(snapshot=stage(self.players))
        for _ in range(3):
            self.room.recover('test fault', self.room.epoch)
            self.reconnect_all()
            self.exchange()
        self.assertIsNone(self.room.stage)
        self.assertEqual(self.room.checkpoint, SAVE)

    def test_spoofed_resume_cannot_take_seat(self):
        attacker = RecoveryClient.__new__(RecoveryClient)
        attacker.address, attacker.token, attacker.game_hash = self.server.address, 'guest-secret', 'a' * 64
        attacker.role, attacker.timeout, attacker.ticket = 'guest', 3, '0' * 64
        with self.assertRaisesRegex(ProtocolError, 'ticket'):
            attacker.connect()
        self.assertIsNone(self.room.error)
        self.assertEqual(self.room.epoch, 0)
        self.exchange()

    def test_normal_quit_keeps_save_and_ends_room(self):
        self.exchange()
        send(self.clients[0].socket, {'type': 'stop'})
        def notice(c):
            with self.assertRaises(SessionEnded):
                c.exchange(0, STATE)
        list(self.pool.map(notice, self.clients))
        self.assertEqual(self.room.checkpoint, SAVE)
        self.assertEqual(self.room.epoch, 0)


class TwoPlayerRecoveryTests(RecoveryTests):
    players = 2


if __name__ == '__main__':
    unittest.main()
