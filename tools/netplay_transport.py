"""Resumable ordered inputs: a lost socket does not destroy a live simulation.

Requests are immutable per (generation, player, frame). Retained replies make
retries idempotent, even when the server committed a frame before its reply was
lost. Only verified simulation divergence or a native restart changes generation.
"""
from collections import deque
import base64
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import socket
import time
import zlib

from netplay_protocol import (Room, VERSION, MAX_INPUT, ProtocolError, TransportError,
                              RecoveryNeeded, SessionEnded, require, receive, send)
from netplay_recovery import RecoveryRoom as StageRoom, RecoveryServer as StageServer
from netplay_recovery import RecoveryClient as StageClient
from netplay_saves import encode, validate
from netplay_stage import validate_stage

TRANSPORT_VERSION = VERSION + '-resume-2'


class RecoveryRoom(StageRoom):
    def __init__(self, *args, history_frames=4096, diagnostics_dir=None, **kwargs):
        super().__init__(*args, **kwargs)
        require(type(history_frames) is int and history_frames >= self.delay + 2, 'Invalid replay history')
        self.history_frames = history_frames
        self.started = False
        self.confirmed_frame = -1
        self.network_events = deque(maxlen=128)
        self.diagnostics_dir = Path(diagnostics_dir) if diagnostics_dir else None
        self.event_number = 0
        self.event_paths = deque()
        self.reported = set()

    def event(self, kind, **data):
        record = {'event': kind, 'epoch': self.epoch, **data}
        self.network_events.append(record)
        if self.diagnostics_dir is not None:
            self.event_number += 1
            try:
                self.diagnostics_dir.mkdir(parents=True, exist_ok=True)
                destination = self.diagnostics_dir / f'{self.event_number:06d}-{kind}.json'
                destination.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
                self.event_paths.append(destination)
                if len(self.event_paths) > 128:
                    self.event_paths.popleft().unlink(missing_ok=True)
            except OSError as error:
                # Diagnostic output must not terminate a playable session.
                print('DIAGNOSTIC_WRITE_FAILED', str(error), flush=True)

    def control(self, packet):
        """A bounded authenticated side channel; it never replaces a live lease."""
        required = {'type', 'version', 'ticket', 'token', 'action', 'epoch'}
        require(type(packet) is dict and required <= set(packet) <= required | {'data'}, 'Invalid control packet')
        require(packet['type'] == 'control' and packet['version'] == TRANSPORT_VERSION, 'Incompatible control')
        require(type(packet['ticket']) is str and type(packet['token']) is str, 'Invalid control credentials')
        with self.condition:
            slots = [p for p, ticket in self.tickets.items() if hmac.compare_digest(packet['ticket'], ticket)]
            require(len(slots) == 1, 'Unknown control ticket')
            slot = slots[0]
            require(hmac.compare_digest(packet['token'], self.host_token if slot == 0 else self.token), 'Incorrect control token')
            epoch = packet['epoch']
            require(type(epoch) is int and max(0, self.epoch - 8) <= epoch <= self.epoch, 'Invalid diagnostic epoch')
            if packet['action'] == 'stop':
                self.stop(f'{slot + 1}P가 게임을 종료했습니다.')
            else:
                require(packet['action'] == 'diagnostic' and type(packet.get('data')) is str, 'Invalid control action')
                key = (epoch, slot)
                if key not in self.reported:
                    require(len(packet['data']) <= 600000, 'Diagnostic too large')
                    try:
                        decoder = zlib.decompressobj()
                        raw = decoder.decompress(base64.b64decode(packet['data'], validate=True), 8 * 1024 * 1024 + 1)
                        require(len(raw) <= 8 * 1024 * 1024 and decoder.eof and not decoder.unused_data, 'Invalid diagnostic size')
                        detail = json.loads(raw)
                    except (ValueError, zlib.error) as error:
                        raise ProtocolError('Invalid diagnostic encoding') from error
                    require(type(detail) is dict and set(detail) <= {'frame', 'state', 'entities', 'recent', 'error', 'characters'}, 'Invalid diagnostic fields')
                    self.reported.add(key)
                    self.reported = {k for k in self.reported if k[0] >= self.epoch - 8}
                    self.event('peer_diagnostic', epoch=epoch, slot=slot, detail=detail)
            return {'type': 'control_ok'}

    def recover(self, reason, epoch):
        with self.condition:
            before = self.epoch
            super().recover(reason, epoch)
            if self.epoch != before:
                self.started = False
                self.confirmed_frame = -1
                self.event('stage_restart', reason=str(reason), stage=None if self.stage is None else self.stage['stage'])

    def attach(self, hello):
        fields = {'type', 'version', 'token', 'role', 'game_hash', 'resume', 'live', 'epoch', 'cursor'}
        require(type(hello) is dict and hello.get('version') == TRANSPORT_VERSION,
                'Protocol mismatch: all players must update their launcher')
        require(set(hello) == fields and hello['type'] == 'hello', 'Invalid hello fields')
        require(type(hello['live']) is bool and type(hello['cursor']) is int and hello['cursor'] >= 0,
                'Invalid resume cursor')
        require(hello['epoch'] is None or type(hello['epoch']) is int and hello['epoch'] >= 0,
                'Invalid resume generation')
        with self.condition:
            resume = hello['resume']
            continued = False
            if resume is None:
                require(not hello['live'] and hello['epoch'] is None and hello['cursor'] == 0, 'Invalid initial hello')
                legacy = {k: hello[k] for k in ('type', 'version', 'token', 'role', 'game_hash')}
                legacy['version'] = VERSION
                slot = Room.join(self, legacy)
                self.tickets[slot] = secrets.token_hex(32)
            else:
                require(type(resume) is str and len(resume) == 64, 'Invalid resume ticket')
                matches = [slot for slot, ticket in self.tickets.items() if hmac.compare_digest(resume, ticket)]
                require(len(matches) == 1, 'Unknown resume ticket')
                slot = matches[0]
                require(hello['role'] == ('host' if slot == 0 else 'guest'), 'Resume role changed')
                secret = self.host_token if slot == 0 else self.token
                require(type(hello['token']) is str and hmac.compare_digest(hello['token'], secret), 'Incorrect room token')
                require(hello['game_hash'] == self.game_hash, 'Game files differ')
                if self.stop_reason:
                    raise SessionEnded(self.stop_reason, self.checkpoint)
                require(not self.error, self.error or 'Room has stopped')
                if hello['live']:
                    if hello['epoch'] != self.epoch or not self.started:
                        raise RecoveryNeeded('스테이지를 다시 불러와야 합니다.')
                    require(hello['cursor'] <= self.expected[slot], 'Resume cursor is ahead of accepted input')
                    oldest = max(0, self.confirmed_frame - self.history_frames + 1)
                    if hello['cursor'] < oldest:
                        self.recover('Retained input history expired', self.epoch)
                        raise RecoveryNeeded('보관된 입력 범위를 넘어 스테이지를 다시 불러옵니다.')
                    continued = True
                else:
                    self.recover(f'{slot + 1}P game restarted', self.epoch)
            lease = secrets.token_hex(16)
            self.leases[slot] = lease
            if not continued:
                self.ready.discard(slot)
            self.condition.notify_all()
            self.check_lease(slot, lease, self.epoch)
            if continued:
                self.event('transport_resumed', slot=slot, cursor=hello['cursor'])
            return slot, lease, self.epoch, continued

    def wait_for(self, predicate, slot, lease, epoch):
        deadline = time.monotonic() + self.timeout
        while not predicate():
            self.check_lease(slot, lease, epoch)
            left = deadline - time.monotonic()
            if left <= 0:
                # The client reconnects and retries the *same* frame. Preserve
                # all accepted inputs and the native game on every PC.
                raise TransportError('Waiting for another player; simulation retained')
            self.condition.wait(min(left, .25))
        self.check_lease(slot, lease, epoch)

    def prepare_generation(self, slot, lease, epoch, packet):
        with self.condition:
            result = super().prepare_generation(slot, lease, epoch, packet)
            self.started = True
            return result

    def submit_generation(self, slot, lease, epoch, packet):
        fields = {'type', 'epoch', 'frame', 'input', 'state'}
        require(type(packet) is dict and fields <= set(packet) <= fields | {'checkpoint', 'detail', 'stage'},
                'Invalid input fields')
        require(packet['type'] == 'input' and packet['epoch'] == epoch, 'Invalid input generation')
        frame, value, digest = packet['frame'], packet['input'], packet['state']
        require(type(frame) is int and frame >= 0 and type(value) is int and 0 <= value <= MAX_INPUT
                and not value & ~MAX_INPUT, 'Invalid input')
        require(slot == 0 or value <= 0x1ff, 'Only the host may control system keys')
        require((frame == 0 and digest is None) or (frame > 0 and type(digest) is str and len(digest) == 64
                and all(c in '0123456789abcdef' for c in digest)), 'Invalid state digest')
        detail = packet.get('detail')
        require(detail is None or type(detail) is dict and len(json.dumps(detail)) <= 4096, 'Invalid state details')
        checkpoint, stage = packet.get('checkpoint'), packet.get('stage')
        if checkpoint is not None:
            require(slot == 0 and frame > 0, 'Invalid save publisher')
            validate(checkpoint)
        if stage is not None:
            require(slot == 0 and frame > 0, 'Invalid stage publisher')
            validate_stage(stage, self.players)
            require(detail is not None and detail.get('stage_checkpoint') == hashlib.sha256(encode(stage)).hexdigest(),
                    'Stage digest mismatch')
        request_hash = hashlib.sha256(encode(packet)).hexdigest()
        with self.condition:
            self.check_lease(slot, lease, epoch)
            require(self.started and len(self.ready) == self.players and (not self.frames or frame < self.frames),
                    'Room is not ready for this frame')
            entry = self.pending.get(frame)
            if entry is not None and slot in entry['requests']:
                require(entry['requests'][slot] == request_hash, 'Conflicting replayed input')
            else:
                require(frame == self.expected[slot], 'Out-of-order input')
                if checkpoint is not None:
                    require(checkpoint['profile'] == self.checkpoint['profile'] and
                            checkpoint['revision'] > self.checkpoint['revision'], 'Invalid save revision')
                if entry is None:
                    entry = self.pending.setdefault(frame, {'requests': {}, 'inputs': {}, 'states': {}, 'details': {},
                                                            'checkpoint': None, 'stage': None, 'reply': None})
                self.expected[slot] += 1
                entry['requests'][slot] = request_hash
                entry['inputs'][slot] = value
                entry['states'][slot] = digest
                entry['details'][slot] = detail
                if checkpoint is not None:
                    entry['checkpoint'] = checkpoint
                if stage is not None:
                    entry['stage'] = stage
                if len(entry['inputs']) == self.players:
                    require(frame == self.confirmed_frame + 1, 'Noncontiguous confirmed input')
                    if len(set(entry['states'].values())) != 1:
                        self.failure_details = {'epoch': epoch, 'frame': frame,
                                                'digests': entry['states'], 'players': entry['details']}
                        self.event('state_mismatch', **self.failure_details)
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
                    self.confirmed_frame = frame
                    inputs = [entry['inputs'].get(p, 0) for p in range(3)]
                    entry['reply'] = {'type': 'frame', 'frame': frame + self.delay, 'inputs': inputs}
                    if entry['checkpoint'] is not None:
                        entry['reply']['checkpoint'] = entry['checkpoint']
                    self.audit.append({'epoch': epoch, 'frame': frame, 'inputs': inputs, 'state': digest})
                    self.audit_count += 1
                    self.pending.pop(frame - self.history_frames, None)
                    self.condition.notify_all()
            self.wait_for(lambda: entry['reply'] is not None, slot, lease, epoch)
            return entry['reply']


class RecoveryServer(StageServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seat_connections = {}

    def handle(self, connection):
        slot = lease = epoch = None
        try:
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            connection.settimeout(10)
            hello = receive(connection, recovery_signal=False)
            if hello.get('type') == 'control':
                send(connection, self.room.control(hello))
                return
            with self.room.condition:
                slot, lease, epoch, continued = self.room.attach(hello)
                with self.lock:
                    previous = self.seat_connections.get(slot)
                    self.seat_connections[slot] = (lease, connection)
                    if previous is not None:
                        try:
                            previous[1].shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        previous[1].close()
                        self.connections.discard(previous[1])
            connection.settimeout(self.room.timeout)
            send(connection, {'type': 'welcome', 'slot': slot, 'players': self.room.players,
                              'frames': self.room.frames, 'delay': self.room.delay, 'epoch': epoch,
                              'resume': self.room.tickets[slot], 'continued': continued})
            if not continued:
                send(connection, self.room.prepare_generation(slot, lease, epoch, receive(connection, recovery_signal=False)))
            while True:
                packet = receive(connection, recovery_signal=False)
                with self.room.condition:
                    self.room.check_lease(slot, lease, epoch)
                    if packet == {'type': 'stop'}:
                        self.room.stop(f'{slot + 1}P가 게임을 종료했습니다.')
                        raise SessionEnded(self.room.stop_reason)
                    if packet == {'type': 'recover'}:
                        self.room.recover(f'{slot + 1}P game restarted', epoch)
                        raise RecoveryNeeded('게임 창을 다시 열고 있습니다.')
                    if packet == {'type': 'done'}:
                        require(self.room.frames and self.room.expected[slot] == self.room.frames, 'Premature completion')
                        self.room.completed.add(slot)
                        self.room.condition.notify_all()
                        self.room.wait_for(lambda: len(self.room.completed) == self.room.players, slot, lease, epoch)
                        send(connection, {'type': 'finished'})
                        return
                send(connection, self.room.submit_generation(slot, lease, epoch, packet))
        except SessionEnded as error:
            self.deliver_end(connection, {'type': 'ended', 'message': str(error), 'checkpoint': self.room.checkpoint})
        except RecoveryNeeded as error:
            self.deliver_end(connection, {'type': 'recover', 'message': str(error)})
        except (TransportError, OSError) as error:
            with self.room.condition:
                if slot is not None and self.room.leases.get(slot) == lease:
                    if not self.room.started:
                        self.room.ready.discard(slot)
                    self.room.event('transport_interrupted', slot=slot, frame=self.room.expected[slot], reason=str(error)[:300])
                    self.room.condition.notify_all()
        except (ProtocolError, ValueError) as error:
            with self.room.condition:
                if slot is not None and self.room.leases.get(slot) == lease:
                    self.room.event('protocol_error', slot=slot, reason=str(error))
                    self.room.abort(str(error))
            self.deliver_end(connection, {'type': 'error', 'message': str(error)})
        finally:
            connection.close()
            with self.lock:
                self.connections.discard(connection)
                if self.seat_connections.get(slot) == (lease, connection):
                    self.seat_connections.pop(slot, None)


class RecoveryClient(StageClient):
    def __init__(self, address, token, game_hash, role='guest', timeout=90, *, status=print, resume_seconds=300):
        self.address, self.token, self.game_hash, self.role, self.timeout = address, token, game_hash, role, timeout
        self.ticket, self.received_checkpoint, self.stage = None, None, None
        self.status, self.resume_seconds = status, resume_seconds
        self.pending = {}
        self.next_result = 0
        self.transport_resumes = 0
        self.connect()

    def connect(self, live=False):
        connection = socket.create_connection(self.address, timeout=min(10, self.timeout))
        connection.settimeout(min(self.timeout, 5) if live else self.timeout)
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            send(connection, {'type': 'hello', 'version': TRANSPORT_VERSION, 'token': self.token,
                               'role': self.role, 'game_hash': self.game_hash, 'resume': self.ticket,
                               'live': live, 'epoch': self.epoch if self.ticket is not None else None,
                               'cursor': self.next_result if live else 0})
            result = receive(connection)
            require(result.get('type') == 'welcome', 'Expected welcome')
            require(type(result.get('players')) is int and result['players'] in (2, 3) and
                    type(result.get('epoch')) is int and result['epoch'] >= 0, 'Invalid room')
            require(type(result.get('slot')) is int and result['slot'] in range(result['players']), 'Invalid player slot')
            require(type(result.get('resume')) is str and len(result['resume']) == 64, 'Invalid resume ticket')
            if self.ticket is not None:
                require(result['resume'] == self.ticket and result['slot'] == self.slot, 'Resume identity changed')
            require(result.get('continued') is live, 'Unexpected continuation mode')
            if live:
                require(result['epoch'] == self.epoch, 'Live resume changed generation')
            else:
                self.slot, self.players, self.frames, self.delay = (result[k] for k in ('slot', 'players', 'frames', 'delay'))
                require(type(self.frames) is int and 0 <= self.frames <= 200000 and
                        type(self.delay) is int and 0 <= self.delay <= 12, 'Invalid frame configuration')
                self.ticket, self.epoch, self.frame = result['resume'], result['epoch'], 0
                self.pending.clear()
                self.next_result = 0
            self.socket = connection
        except BaseException:
            connection.close()
            raise

    def ready(self, header=None):
        result = super().ready(header)
        self.socket.settimeout(min(self.timeout, 5))
        return result

    def _resume_transport(self, deadline):
        self.close()
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            try:
                self.connect(live=True)
                self.socket.settimeout(min(self.timeout, 5))
                for frame in sorted(self.pending):
                    send(self.socket, self.pending[frame])
                self.transport_resumes += 1
                return
            except (TransportError, OSError):
                self.close()
                time.sleep(min(.5, max(0, deadline - time.monotonic())))
        raise ProtocolError('연결 복구 시간이 초과됐습니다. 마지막 확인된 저장본은 유지됩니다.')

    def exchange(self, local_input, state, *, checkpoint=None, detail=None, stage=None):
        packet = {'type': 'input', 'epoch': self.epoch, 'frame': self.frame, 'input': local_input, 'state': state}
        for key, value in (('checkpoint', checkpoint), ('detail', detail), ('stage', stage)):
            if value is not None:
                packet[key] = value
        # Copy now so a caller cannot mutate an already transmitted retry.
        self.pending[self.frame] = json.loads(encode(packet))
        deadline = None
        replayed = False
        while True:
            try:
                if not replayed:
                    send(self.socket, self.pending[self.frame])
                if self.frame < self.delay:
                    inputs = [0, 0, 0]
                else:
                    inputs = self.read_frame(self.next_result + self.delay)
                    del self.pending[self.next_result]
                    self.next_result += 1
                self.frame += 1
                if deadline is not None:
                    self.status('연결이 복구됐습니다. 게임을 재실행하지 않고 계속합니다.')
                return inputs
            except (TransportError, OSError):
                if deadline is None:
                    deadline = time.monotonic() + self.resume_seconds
                    self.status('연결을 기다리고 있습니다. 현재 게임과 입력 순서를 유지합니다…')
                self._resume_transport(deadline)
                replayed = True

    def finish(self):
        if not self.frames:
            try:
                self.control('stop')
            except (ProtocolError, OSError):
                self.status('게임을 종료했습니다. 연결이 닿지 않아 다른 참가자는 연결 대기 후 종료할 수 있습니다.')
            return  # Closing a game must never launch a replacement game.
        require(self.frame == self.frames, 'Game ended early')
        while self.next_result < self.frames:
            self.read_frame(self.next_result + self.delay)
            del self.pending[self.next_result]
            self.next_result += 1
        send(self.socket, {'type': 'done'})
        require(receive(self.socket).get('type') == 'finished', 'Expected session completion')

    def control(self, action, data=None):
        packet = {'type': 'control', 'version': TRANSPORT_VERSION, 'ticket': self.ticket,
                  'token': self.token, 'action': action, 'epoch': self.epoch}
        if data is not None:
            packet['data'] = data
        with socket.create_connection(self.address, timeout=min(self.timeout, 3)) as connection:
            connection.settimeout(min(self.timeout, 3))
            send(connection, packet)
            require(receive(connection).get('type') == 'control_ok', 'Unconfirmed control request')

    def report_failure(self, diagnostic):
        # Keep the frame history nearest the error if a very busy stage grows
        # beyond the limit. Diagnostics cannot make a recoverable error fatal.
        try:
            report = dict(diagnostic)
            report['recent'] = list(report.get('recent', []))
            while True:
                raw = encode(report)
                packed = base64.b64encode(zlib.compress(raw, 1)).decode('ascii')
                if len(raw) <= 8 * 1024 * 1024 and len(packed) <= 600000:
                    break
                if report['recent']:
                    report['recent'].pop(0)
                else:
                    return False
            self.control('diagnostic', packed)
            return True
        except (ProtocolError, OSError, ValueError):
            return False

    def close(self):
        connection = getattr(self, 'socket', None)
        if connection is not None:
            connection.close()
