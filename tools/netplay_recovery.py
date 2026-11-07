"""Persistent seats and coordinated stage restart, independent of game processes.

Every socket owns a lease. A new generation discards *all* queued inputs; no
input from a disconnected simulation can enter the replacement simulation.
"""
from collections import deque
import hashlib
import hmac
import json
import secrets
import socket
import time

from netplay_protocol import (Room, Server, Client, ProtocolError, TransportError,
                              RecoveryNeeded, SessionEnded, require, receive, send,
                              MAX_INPUT, VERSION)
from netplay_saves import validate, encode
from netplay_stage import validate_stage

RECOVERY_VERSION = VERSION + '-recovery-1'


class RecoveryRoom(Room):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.epoch = 0
        self.leases, self.tickets = {}, {}
        self.recovering = False
        self.stage = None
        self.recovery_history = deque(maxlen=64)
        self.recent_failures = deque()

    def check_lease(self, slot, lease, epoch):
        require(self.leases.get(slot) == lease, 'Superseded connection')
        if epoch != self.epoch:
            raise RecoveryNeeded('연결을 복구하고 스테이지를 다시 불러옵니다.')
        if self.error:
            raise ProtocolError(self.error)
        if self.stop_reason:
            raise SessionEnded(self.stop_reason, self.checkpoint)

    def recover(self, reason, epoch):
        with self.condition:
            if epoch != self.epoch or self.recovering or self.stop_reason or self.error:
                return
            now = time.monotonic()
            while self.recent_failures and self.recent_failures[0] < now - 120:
                self.recent_failures.popleft()
            self.recent_failures.append(now)
            if len(self.recent_failures) >= 8:
                self.stop('복구 실패가 반복되었습니다. 저장본은 유지됩니다. 실행기를 다시 열어주세요.')
                return
            self.epoch += 1
            self.recovering = True
            if len(self.recent_failures) >= 3:
                self.stage = None  # Escape a reproducible stage crash via the title.
            self.recovery_history.append({'epoch': self.epoch, 'reason': str(reason),
                                          'stage': None if self.stage is None else self.stage['stage']})
            self.ready.clear()
            self.pending.clear()
            self.expected = [0] * self.players
            self.completed.clear()
            self.condition.notify_all()

    def attach(self, hello):
        require(type(hello) is dict and hello.get('version') == RECOVERY_VERSION,
                'Protocol mismatch: all players must use the same recovery release')
        require(set(hello) == {'type', 'version', 'token', 'role', 'game_hash', 'resume'}, 'Invalid hello fields')
        resume = hello['resume']
        with self.condition:
            if resume is None:
                legacy = {key: value for key, value in hello.items() if key != 'resume'}
                legacy['version'] = VERSION
                slot = super().join(legacy)
                self.tickets[slot] = secrets.token_hex(32)
            else:
                require(hello['type'] == 'hello' and type(resume) is str and len(resume) == 64 and
                        all(c in '0123456789abcdef' for c in resume), 'Invalid resume ticket')
                matches = [slot for slot, ticket in self.tickets.items() if hmac.compare_digest(resume, ticket)]
                require(len(matches) == 1, 'Unknown resume ticket')
                slot = matches[0]
                require(hello['role'] == ('host' if slot == 0 else 'guest'), 'Resume role changed')
                secret = self.host_token if slot == 0 else self.token
                require(type(hello['token']) is str and hmac.compare_digest(hello['token'], secret), 'Incorrect room token')
                require(hello['game_hash'] == self.game_hash, 'Game files differ')
                self.recover(f'{slot + 1}P reconnected', self.epoch)
            lease = secrets.token_hex(16)
            self.ready.discard(slot)
            self.leases[slot] = lease
            self.condition.notify_all()
            self.check_lease(slot, lease, self.epoch)
            return slot, lease, self.epoch

    def wait_for(self, predicate, slot, lease, epoch):
        deadline = time.monotonic() + self.timeout
        while not predicate():
            self.check_lease(slot, lease, epoch)
            left = deadline - time.monotonic()
            if left <= 0:
                self.recover(f'{slot + 1}P waiting for peer', epoch)
                raise RecoveryNeeded('다른 플레이어의 연결을 기다리고 있습니다.')
            self.condition.wait(min(left, .5))
        self.check_lease(slot, lease, epoch)

    def prepare_generation(self, slot, lease, epoch, packet):
        with self.condition:
            self.check_lease(slot, lease, epoch)
            require(packet.get('type') == 'ready' and packet.get('epoch') == epoch and
                    set(packet) <= {'type', 'epoch', 'checkpoint'}, 'Invalid ready packet')
            if 'checkpoint' in packet:
                require(slot == 0, 'Only the host may initialize saves')
                validate(packet['checkpoint'])
                if self.checkpoint is None:
                    self.checkpoint = packet['checkpoint']
                else:
                    require(packet['checkpoint']['profile'] == self.checkpoint['profile'] and
                            packet['checkpoint']['revision'] <= self.checkpoint['revision'], 'Save bootstrap changed')
            self.ready.add(slot)
            self.condition.notify_all()
            self.wait_for(lambda: len(self.ready) == self.players and self.checkpoint is not None, slot, lease, epoch)
            self.recovering = False
            return {'type': 'start', 'epoch': epoch, 'header': encode(self.checkpoint).hex(), 'stage': self.stage}

    def submit_generation(self, slot, lease, epoch, packet):
        fields = {'type', 'epoch', 'frame', 'input', 'state'}
        require(fields <= set(packet) <= fields | {'checkpoint', 'detail', 'stage'}, 'Invalid input fields')
        require(packet['type'] == 'input' and packet['epoch'] == epoch, 'Invalid input generation')
        frame, value, digest = packet['frame'], packet['input'], packet['state']
        require(type(frame) is int and type(value) is int and 0 <= value <= MAX_INPUT and not value & ~MAX_INPUT, 'Invalid input')
        require(slot == 0 or value <= 0x1ff, 'Only the host may control system keys')
        require((frame == 0 and digest is None) or (frame > 0 and type(digest) is str and
                len(digest) == 64 and all(c in '0123456789abcdef' for c in digest)), 'Invalid state digest')
        detail = packet.get('detail')
        require(detail is None or (type(detail) is dict and len(json.dumps(detail)) <= 4096), 'Invalid state details')
        checkpoint, stage = packet.get('checkpoint'), packet.get('stage')
        if checkpoint is not None:
            require(slot == 0 and frame > 0, 'Invalid save publisher')
            validate(checkpoint)
        if stage is not None:
            require(slot == 0 and frame > 0, 'Invalid stage publisher')
            validate_stage(stage, self.players)
            expected = hashlib.sha256(encode(stage)).hexdigest()
            require(detail is not None and detail.get('stage_checkpoint') == expected, 'Stage digest mismatch')
        with self.condition:
            self.check_lease(slot, lease, epoch)
            require(len(self.ready) == self.players and frame == self.expected[slot] and
                    (not self.frames or frame < self.frames), 'Out-of-order input')
            if checkpoint is not None:
                require(checkpoint['profile'] == self.checkpoint['profile'] and
                        checkpoint['revision'] > self.checkpoint['revision'], 'Invalid save revision')
            self.expected[slot] += 1
            entry = self.pending.setdefault(frame, {'inputs': {}, 'states': {}, 'details': {}, 'readers': set(),
                                                     'checkpoint': None, 'stage': None})
            entry['inputs'][slot] = value
            entry['states'][slot] = digest
            entry['details'][slot] = detail
            if checkpoint is not None:
                entry['checkpoint'] = checkpoint
            if stage is not None:
                entry['stage'] = stage
            if len(entry['inputs']) == self.players:
                if len(set(entry['states'].values())) != 1:
                    self.failure_details = {'epoch': epoch, 'frame': frame, 'digests': entry['states'], 'players': entry['details']}
                    self.recover(f'Game state diverged at frame {frame}', epoch)
                    raise RecoveryNeeded('게임 진행 차이를 감지했습니다. 스테이지를 다시 불러옵니다.')
                if entry['stage'] is not None:
                    expected = hashlib.sha256(encode(entry['stage'])).hexdigest()
                    require(all(isinstance(v, dict) and v.get('stage_checkpoint') == expected
                                for v in entry['details'].values()), 'Players disagree on stage checkpoint')
                    self.stage = entry['stage']
                elif frame > 0 and all(isinstance(v, dict) and v.get('scene') != 0 for v in entry['details'].values()):
                    self.stage = None
                if entry['checkpoint'] is not None:
                    self.checkpoint = entry['checkpoint']
                self.audit.append({'epoch': epoch, 'frame': frame, 'inputs': [entry['inputs'].get(p, 0) for p in range(3)], 'state': digest})
                self.audit_count += 1
                self.condition.notify_all()
            self.wait_for(lambda: len(entry['inputs']) == self.players, slot, lease, epoch)
            result = [entry['inputs'].get(p, 0) for p in range(3)]
            entry['readers'].add(slot)
            if len(entry['readers']) == self.players:
                del self.pending[frame]
            return result


class RecoveryServer(Server):
    def handle(self, connection):
        slot = lease = epoch = None
        try:
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            connection.settimeout(10)
            slot, lease, epoch = self.room.attach(receive(connection, recovery_signal=False))
            connection.settimeout(self.room.timeout)
            send(connection, {'type': 'welcome', 'slot': slot, 'players': self.room.players, 'frames': self.room.frames,
                              'delay': self.room.delay, 'epoch': epoch, 'resume': self.room.tickets[slot]})
            send(connection, self.room.prepare_generation(slot, lease, epoch, receive(connection, recovery_signal=False)))
            for frame in range(self.room.delay):
                send(connection, {'type': 'frame', 'frame': frame, 'inputs': [0, 0, 0]})
            frame, last_checkpoint = 0, None
            while not self.room.frames or frame < self.room.frames:
                packet = receive(connection, recovery_signal=False)
                with self.room.condition:
                    self.room.check_lease(slot, lease, epoch)
                    if packet == {'type': 'stop'}:
                        self.room.stop(f'{slot + 1}P가 게임을 종료했습니다.')
                        raise SessionEnded(self.room.stop_reason)
                    if packet == {'type': 'recover'}:
                        self.room.recover(f'{slot + 1}P game restarted', epoch)
                        raise RecoveryNeeded('게임 창을 다시 열고 있습니다.')
                inputs = self.room.submit_generation(slot, lease, epoch, packet)
                response = {'type': 'frame', 'frame': frame + self.room.delay, 'inputs': inputs}
                latest = self.room.checkpoint
                if latest is not last_checkpoint:
                    response['checkpoint'] = latest
                    last_checkpoint = latest
                send(connection, response)
                frame += 1
            require(receive(connection) == {'type': 'done'}, 'Missing completion')
            with self.room.condition:
                self.room.completed.add(slot)
                self.room.condition.notify_all()
                self.room.wait_for(lambda: len(self.room.completed) == self.room.players, slot, lease, epoch)
            send(connection, {'type': 'finished'})
        except SessionEnded as exc:
            self.deliver_end(connection, {'type': 'ended', 'message': str(exc), 'checkpoint': self.room.checkpoint})
        except (RecoveryNeeded, TransportError, OSError) as exc:
            if slot is not None:
                with self.room.condition:
                    if self.room.leases.get(slot) == lease:
                        self.room.recover(str(exc), epoch)
                        self.room.ready.discard(slot)
                        self.room.condition.notify_all()
            self.deliver_end(connection, {'type': 'recover', 'message': str(exc)})
        except (ProtocolError, ValueError) as exc:
            if slot is not None and self.room.leases.get(slot) == lease:
                self.room.abort(str(exc))
            self.deliver_end(connection, {'type': 'error', 'message': str(exc)})
        finally:
            connection.close()
            with self.lock:
                self.connections.discard(connection)

    @staticmethod
    def deliver_end(connection, packet):
        try:
            send(connection, packet)
            connection.shutdown(socket.SHUT_WR)
            connection.settimeout(.5)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and connection.recv(4096):
                pass
        except (ProtocolError, OSError):
            pass


class RecoveryClient(Client):
    supports_recovery = True

    def __init__(self, address, token, game_hash, role='guest', timeout=90):
        self.address, self.token, self.game_hash, self.role, self.timeout = address, token, game_hash, role, timeout
        self.ticket = None
        self.received_checkpoint = None
        self.stage = None
        self.connect()

    def connect(self):
        self.socket = socket.create_connection(self.address, timeout=10)
        self.socket.settimeout(self.timeout)
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            send(self.socket, {'type': 'hello', 'version': RECOVERY_VERSION, 'token': self.token,
                               'role': self.role, 'game_hash': self.game_hash, 'resume': self.ticket})
            result = receive(self.socket)
            require(result.get('type') == 'welcome', 'Expected welcome')
            require(type(result.get('players')) is int and result['players'] in (2, 3) and
                    type(result.get('epoch')) is int and result['epoch'] >= 0, 'Invalid room')
            require(type(result.get('slot')) is int and result['slot'] in range(result['players']), 'Invalid player slot')
            require(type(result.get('resume')) is str and len(result['resume']) == 64, 'Invalid resume ticket')
            if self.ticket is not None:
                require(result['resume'] == self.ticket and result['slot'] == self.slot, 'Resume identity changed')
            self.slot, self.players, self.frames, self.delay = (result[k] for k in ('slot', 'players', 'frames', 'delay'))
            require(type(self.frames) is int and 0 <= self.frames <= 200000 and type(self.delay) is int and 0 <= self.delay <= 12, 'Invalid frame configuration')
            self.ticket, self.epoch, self.frame = result['resume'], result['epoch'], 0
        except Exception:
            self.socket.close()
            raise

    def ready(self, header=None):
        packet = {'type': 'ready', 'epoch': self.epoch}
        if header is not None and (self.epoch == 0 or self.received_checkpoint is None):
            packet['checkpoint'] = json.loads(header)
        send(self.socket, packet)
        result = receive(self.socket)
        require(result.get('type') == 'start' and result.get('epoch') == self.epoch, 'Invalid start generation')
        checkpoint = json.loads(bytes.fromhex(result['header']))
        validate(checkpoint)
        self.stage = result.get('stage')
        if self.stage is not None:
            validate_stage(self.stage, self.players)
        self.received_checkpoint = checkpoint
        return encode(checkpoint)

    def exchange(self, local_input, state, *, checkpoint=None, detail=None, stage=None):
        packet = {'type': 'input', 'epoch': self.epoch, 'frame': self.frame, 'input': local_input, 'state': state}
        for key, value in (('checkpoint', checkpoint), ('detail', detail), ('stage', stage)):
            if value is not None:
                packet[key] = value
        try:
            send(self.socket, packet)
            result = self.read_frame(self.frame)
        except TransportError as exc:
            raise RecoveryNeeded('연결이 끊어져 다시 접속합니다.') from exc
        self.frame += 1
        return result

    def reconnect(self, initial=None, status=print, deadline_seconds=300):
        self.close()
        deadline, attempt = time.monotonic() + deadline_seconds, 0
        while True:
            attempt += 1
            try:
                self.connect()
                return self.ready(initial)
            except (TransportError, RecoveryNeeded, OSError):
                self.close()
                if time.monotonic() >= deadline:
                    raise ProtocolError('5분 동안 연결을 복구하지 못했습니다. 저장본은 유지됩니다.')
                status(f'다시 접속하는 중… ({attempt})')
                time.sleep(min(5, attempt, max(0, deadline - time.monotonic())))

    def request_recovery(self):
        try:
            send(self.socket, {'type': 'recover'})
        except TransportError:
            pass
