"""RBO EX3 experimental lockstep protocol. Inputs belong to connections, not keys."""
import hashlib
from collections import deque
import hmac
import json
from pathlib import Path
import socket
import struct
import threading
import time

VERSION = 'rbo-ex3-lab-7.0'
MAX_MESSAGE = 768 * 1024
MAX_INPUT = 0x3001ff  # nine native actions plus host F12/Escape
EXPECTED_EXE = '05b14b047e49dabb026c96150f8d5aab497d20d9b2d52f5e59a8aef1af5b8dc0'


class ProtocolError(RuntimeError):
    pass


class TransportError(ProtocolError):
    """A socket interruption, distinct from invalid or incompatible messages."""


class RecoveryNeeded(ProtocolError):
    """The coordinator invalidated this simulation; restart from its checkpoint."""
    def __init__(self, message, mode='restart'):
        super().__init__(message)
        self.mode = mode


class GameCrashed(ProtocolError):
    pass


class PreparedRestart(GameCrashed):
    """The new generation is ready, but this process must be replaced before frame zero."""


class SessionEnded(ProtocolError):
    """An unlimited session was deliberately closed by a participant."""
    def __init__(self, message, checkpoint=None):
        super().__init__(message)
        self.checkpoint = checkpoint


def require(condition, message):
    if not condition:
        raise ProtocolError(message)


def receive_exact(sock, length):
    data = bytearray()
    while len(data) < length:
        try:
            part = sock.recv(length - len(data))
        except OSError as exc:
            raise TransportError(f'Connection closed or timed out: {exc}') from exc
        if not part:
            raise TransportError('Connection closed')
        data.extend(part)
    return bytes(data)


def receive(sock, *, recovery_signal=True):
    length = struct.unpack('!I', receive_exact(sock, 4))[0]
    require(0 < length <= MAX_MESSAGE, 'Message too large')
    try:
        result = json.loads(receive_exact(sock, length))
    except (ValueError, UnicodeError) as exc:
        raise ProtocolError('Invalid JSON') from exc
    require(type(result) is dict, 'Expected an object')
    if recovery_signal and result.get('type') == 'recover':
        mode = result.get('mode', 'restart')
        require(mode in ('restart', 'reload'), 'Invalid recovery instruction')
        raise RecoveryNeeded(str(result.get('message', 'Reloading the stage')), mode)
    if result.get('type') == 'error':
        raise ProtocolError(str(result.get('message', 'Session stopped')))
    if result.get('type') == 'ended':
        checkpoint = result.get('checkpoint')
        if checkpoint is not None:
            from netplay_saves import validate
            try:
                validate(checkpoint)
            except ValueError as exc:
                raise ProtocolError(str(exc)) from exc
        raise SessionEnded(str(result.get('message', 'Session ended')), checkpoint)
    return result


def send(sock, value):
    data = json.dumps(value, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    require(len(data) <= MAX_MESSAGE, 'Message too large')
    try:
        sock.sendall(struct.pack('!I', len(data)) + data)
    except OSError as exc:
        raise TransportError(f'Connection closed or timed out: {exc}') from exc


def fingerprint(directory):
    """Hash actual executable and PAC contents; config and saves stay local."""
    directory = Path(directory)
    exe = directory / 'rbo_ex3.exe'
    require(hashlib.sha256(exe.read_bytes()).hexdigest() == EXPECTED_EXE,
            'This bridge supports only the verified Japanese EX3 4.0 executable')
    files = [exe] + sorted((directory / 'DATA').glob('*.pac'), key=lambda p: p.name.lower())
    require(len(files) > 1, 'No game data found')
    digest = hashlib.sha256()
    from netplay_korean import pack_hash
    digest.update(pack_hash().encode('ascii'))
    for path in files:
        digest.update(path.name.lower().encode('utf-8') + b'\0')
        with path.open('rb') as source:
            for block in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(block)
    return digest.hexdigest()


class Room:
    """Two or three authenticated connections, one immutable slot each."""
    def __init__(self, token, host_token, frames=0, delay=3, timeout=90, players=3):
        require(type(players) is int and players in (2, 3), 'Player count must be 2 or 3')
        require(type(frames) is int and 0 <= frames <= 200000, 'Invalid frame limit')
        require(type(delay) is int and 0 <= delay <= 12, 'Invalid input delay')
        require(bool(token) and bool(host_token) and token != host_token, 'Use distinct room and host tokens')
        self.token, self.host_token = token, host_token
        self.players = players
        self.frames, self.delay, self.timeout = frames, delay, timeout
        self.condition = threading.Condition()
        self.members, self.ready, self.pending = {}, set(), {}
        self.expected = [0] * players
        self.game_hash = None
        self.header = None
        self.error = None
        self.stop_reason = None
        self.completed = set()
        self.audit = deque(maxlen=4096) if frames == 0 else []
        self.audit_count = 0
        self.failure_details = None
        self.checkpoint = None

    def stop(self, message):
        with self.condition:
            self.stop_reason = self.stop_reason or str(message)
            self.condition.notify_all()

    def abort(self, message):
        with self.condition:
            self.error = self.error or str(message)
            self.condition.notify_all()

    def wait(self, predicate):
        deadline = time.monotonic() + self.timeout
        while not predicate():
            if self.error:
                raise ProtocolError(self.error)
            if self.stop_reason:
                raise SessionEnded(self.stop_reason)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.abort('Timed out waiting for another player')
                raise ProtocolError(self.error)
            self.condition.wait(min(remaining, 1))
        if self.error:
            raise ProtocolError(self.error)
        if self.stop_reason:
            raise SessionEnded(self.stop_reason)

    def join(self, hello):
        require(set(hello) == {'type', 'version', 'token', 'role', 'game_hash'}, 'Invalid hello fields')
        require(hello['type'] == 'hello' and hello['version'] == VERSION,
                'Protocol mismatch: update both host and guests to v0.4.0')
        require(type(hello['token']) is str and type(hello['game_hash']) is str, 'Invalid hello values')
        require(hello['token'].isascii() and len(hello['token']) <= 128, 'Invalid room token')
        require(len(hello['game_hash']) == 64 and all(c in '0123456789abcdef' for c in hello['game_hash']), 'Invalid game hash')
        require(hello['role'] in ('host', 'guest'), 'Invalid role')
        secret = self.host_token if hello['role'] == 'host' else self.token
        require(hmac.compare_digest(hello['token'], secret), 'Incorrect room token')
        with self.condition:
            require(not self.error and not self.stop_reason, 'Room has stopped')
            if hello['role'] == 'host':
                require(0 not in self.members, 'Host already connected')
                slot = 0
            else:
                available = [n for n in range(1, self.players) if n not in self.members]
                require(bool(available), 'Room is full')
                slot = available[0]
            require(self.game_hash in (None, hello['game_hash']), 'Game files differ')
            self.game_hash = hello['game_hash']
            self.members[slot] = True
            self.condition.notify_all()
            return slot

    def prepare(self, slot, message):
        fields = {'type', 'header'} if slot == 0 else {'type'}
        require(set(message) == fields and message['type'] == 'ready', 'Invalid ready packet')
        with self.condition:
            require(slot not in self.ready, 'Already ready')
            if slot == 0:
                try:
                    header = bytes.fromhex(message['header'])
                except (ValueError, TypeError) as exc:
                    raise ProtocolError('Invalid initial state') from exc
                require(0 < len(header) <= 256 * 1024, 'Invalid initial state length')
                self.header = header.hex()
            self.ready.add(slot)
            self.condition.notify_all()
            self.wait(lambda: len(self.ready) == self.players)
            return self.header

    def submit(self, slot, packet):
        # No client-supplied slot or three-player array is accepted, even if valid.
        required = {'type', 'frame', 'input', 'state'}
        require(required <= set(packet) <= required | {'checkpoint', 'detail'}, 'Invalid input fields')
        require(packet['type'] == 'input', 'Expected input')
        frame, value, state = packet['frame'], packet['input'], packet['state']
        require(type(frame) is int and type(value) is int and 0 <= value <= MAX_INPUT and not value & ~MAX_INPUT, 'Invalid input value')
        require(slot == 0 or value <= 0x1ff, 'Only the host may control system keys')
        require(state is None or (type(state) is str and len(state) == 64 and all(c in '0123456789abcdef' for c in state)), 'Invalid state digest')
        require((frame == 0) == (state is None), 'Missing state digest')
        detail = packet.get('detail')
        require(detail is None or (type(detail) is dict and len(json.dumps(detail)) <= 4096), 'Invalid state detail')
        checkpoint = packet.get('checkpoint')
        if checkpoint is not None:
            require(slot == 0, 'Only the host may publish a save')
            from netplay_saves import validate
            try:
                validate(checkpoint)
            except ValueError as exc:
                raise ProtocolError(str(exc)) from exc
        with self.condition:
            require(slot in self.ready and len(self.ready) == self.players, 'Room is not ready')
            require(frame == self.expected[slot] and (not self.frames or frame < self.frames), 'Out-of-order input')
            if checkpoint is not None and self.checkpoint is not None:
                require(checkpoint['profile'] == self.checkpoint['profile'], 'Save profile changed')
                require(checkpoint['revision'] > self.checkpoint['revision'], 'Save revision did not advance')
            self.expected[slot] += 1
            entry = self.pending.setdefault(frame, {'inputs': {}, 'states': {}, 'details': {}, 'checkpoint': None, 'readers': set()})
            entry['inputs'][slot] = value
            entry['states'][slot] = state
            entry['details'][slot] = detail
            if checkpoint is not None:
                entry['checkpoint'] = checkpoint
            if len(entry['inputs']) == self.players:
                if len(set(entry['states'].values())) != 1:
                    self.failure_details = {'frame': frame, 'digests': entry['states'], 'players': entry['details']}
                    self.abort(f'Game state diverged at frame {frame}')
                if entry['checkpoint'] is not None and not self.error:
                    self.checkpoint = entry['checkpoint']
                self.audit.append({'frame': frame, 'applied_frame': frame + self.delay,
                                   'inputs': [entry['inputs'].get(p, 0) for p in range(3)], 'state': state})
                self.audit_count += 1
                self.condition.notify_all()
            self.wait(lambda: len(entry['inputs']) == self.players)
            result = [entry['inputs'].get(p, 0) for p in range(3)]
            entry['readers'].add(slot)
            if len(entry['readers']) == self.players:
                del self.pending[frame]
            return result


class Server:
    def __init__(self, room, address=('127.0.0.1', 47624)):
        self.room = room
        self.socket = socket.socket()
        # Do not allow another listener to share the port on Windows.
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        self.socket.bind(address)
        self.socket.listen(4)
        self.socket.settimeout(0.25)
        self.address = self.socket.getsockname()
        self.stopping = threading.Event()
        self.connections, self.threads = set(), []
        self.lock = threading.Lock()

    def serve(self):
        while not self.stopping.is_set():
            try:
                connection, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self.lock:
                if len(self.connections) >= 8:
                    connection.close()
                    continue
                self.connections.add(connection)
            thread = threading.Thread(target=self.handle, args=(connection,), daemon=True)
            self.threads = [worker for worker in self.threads if worker.is_alive()]
            self.threads.append(thread)
            thread.start()

    def handle(self, connection):
        slot = None
        try:
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            connection.settimeout(10)
            slot = self.room.join(receive(connection))
            connection.settimeout(self.room.timeout)
            send(connection, {'type': 'welcome', 'slot': slot, 'players': self.room.players,
                              'frames': self.room.frames, 'delay': self.room.delay})
            header = self.room.prepare(slot, receive(connection))
            send(connection, {'type': 'start', 'header': header})
            for frame in range(self.room.delay):
                send(connection, {'type': 'frame', 'frame': frame, 'inputs': [0, 0, 0]})
            frame = 0
            last_checkpoint = None
            while not self.room.frames or frame < self.room.frames:
                packet = receive(connection)
                if not self.room.frames and packet == {'type': 'stop'}:
                    self.room.stop(f'Player {slot + 1} ended the session')
                    raise SessionEnded(self.room.stop_reason)
                inputs = self.room.submit(slot, packet)
                response = {'type': 'frame', 'frame': frame + self.room.delay, 'inputs': inputs}
                # Checkpoints are immutable and small. Repeat the latest until
                # each connection has received it, without per-frame duplication.
                latest = self.room.checkpoint
                if latest is not None and latest is not last_checkpoint:
                    response['checkpoint'] = latest
                    last_checkpoint = latest
                send(connection, response)
                frame += 1
            require(receive(connection) == {'type': 'done'}, 'Missing completion packet')
            with self.room.condition:
                self.room.completed.add(slot)
                self.room.condition.notify_all()
                self.room.wait(lambda: len(self.room.completed) == self.room.players)
            send(connection, {'type': 'finished'})
        except SessionEnded as exc:
            try:
                response = {'type': 'ended', 'message': str(exc)}
                if self.room.checkpoint is not None:
                    response['checkpoint'] = self.room.checkpoint
                send(connection, response)
                connection.shutdown(socket.SHUT_WR)
                connection.settimeout(1)
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline and connection.recv(4096):
                    pass
            except (OSError, ProtocolError):
                pass
        except (OSError, ProtocolError) as exc:
            if slot is not None:
                self.room.abort(f'Player {slot + 1}: {exc}')
            try:
                send(connection, {'type': 'error', 'message': str(exc)})
                # Preserve the error packet on Windows: closing a socket with
                # pipelined unread inputs can otherwise send RST and lose it.
                connection.shutdown(socket.SHUT_WR)
                connection.settimeout(1)
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline and connection.recv(4096):
                    pass
            except (OSError, ProtocolError):
                pass
        finally:
            connection.close()
            with self.lock:
                self.connections.discard(connection)

    def close(self):
        self.stopping.set()
        self.socket.close()
        if self.room.stop_reason and not self.room.error:
            # The host launcher can finish before guests read the final packet.
            # Let handlers deliver the stop/checkpoint and drain queued inputs
            # before shutting their sockets down (avoids Windows RST).
            deadline = time.monotonic() + 3
            for thread in self.threads:
                thread.join(timeout=max(0, deadline - time.monotonic()))
        else:
            self.room.abort('Server closed')
        with self.lock:
            for connection in self.connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        for thread in self.threads:
            thread.join(timeout=2)


class Client:
    def __init__(self, address, token, game_hash, role='guest', timeout=90):
        self.socket = socket.create_connection(address, timeout=10)
        self.socket.settimeout(timeout)
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            send(self.socket, {'type': 'hello', 'version': VERSION, 'token': token, 'role': role, 'game_hash': game_hash})
            message = receive(self.socket)
            require(message.get('type') == 'welcome', 'Expected welcome')
            self.slot, self.frames, self.delay = message['slot'], message['frames'], message['delay']
            self.players = message.get('players')
            require(type(self.players) is int and self.players in (2, 3), 'Invalid player count')
            require(type(self.slot) is int and 0 <= self.slot < self.players, 'Invalid assigned slot')
            require(type(self.frames) is int and 0 <= self.frames <= 200000, 'Invalid frame count')
            require(type(self.delay) is int and 0 <= self.delay <= 12, 'Invalid delay')
            self.frame = 0
            self.received_checkpoint = None
        except Exception:
            self.socket.close()
            raise

    def ready(self, header=None):
        message = {'type': 'ready'}
        if self.slot == 0:
            require(type(header) is bytes and 0 < len(header) <= 256 * 1024, 'Host initial state required')
            message['header'] = header.hex()
        send(self.socket, message)
        result = receive(self.socket)
        require(result.get('type') == 'start', 'Expected start')
        try:
            header = bytes.fromhex(result['header'])
        except (ValueError, KeyError, TypeError) as exc:
            raise ProtocolError('Invalid initial state') from exc
        require(0 < len(header) <= 256 * 1024, 'Invalid initial state')
        return header

    def read_frame(self, frame):
        try:
            packet = receive(self.socket)
        except SessionEnded as exc:
            if exc.checkpoint is not None:
                self.received_checkpoint = exc.checkpoint
            raise
        require(packet.get('type') == 'frame' and packet.get('frame') == frame, 'Unexpected server frame')
        inputs = packet.get('inputs')
        require(type(inputs) is list and len(inputs) == 3 and all(type(v) is int and 0 <= v <= MAX_INPUT and not v & ~MAX_INPUT for v in inputs), 'Invalid server inputs')
        require(all(value == 0 for value in inputs[self.players:]), 'Input for an inactive player')
        if 'checkpoint' in packet:
            from netplay_saves import validate
            try:
                validate(packet['checkpoint'])
            except ValueError as exc:
                raise ProtocolError(str(exc)) from exc
            self.received_checkpoint = packet['checkpoint']
        return inputs

    def exchange(self, local_input, state, *, checkpoint=None, detail=None):
        require(not self.frames or self.frame < self.frames, 'Frame limit reached')
        packet = {'type': 'input', 'frame': self.frame, 'input': local_input, 'state': state}
        if checkpoint is not None:
            packet['checkpoint'] = checkpoint
        if detail is not None:
            packet['detail'] = detail
        send(self.socket, packet)
        inputs = self.read_frame(self.frame)
        self.frame += 1
        return inputs

    def finish(self):
        if not self.frames:
            send(self.socket, {'type': 'stop'})
            try:
                frame = self.frame
                while True:
                    self.read_frame(frame)
                    frame += 1
            except SessionEnded:
                return
        require(self.frame == self.frames, 'Game ended early')
        for frame in range(self.frames, self.frames + self.delay):
            self.read_frame(frame)
        send(self.socket, {'type': 'done'})
        require(receive(self.socket) == {'type': 'finished'}, 'Expected session completion')

    def close(self):
        self.socket.close()
