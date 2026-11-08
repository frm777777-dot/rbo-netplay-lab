"""Experimental EX3 game bridge: local 1P mapping -> connection-owned player slot.

Bootstraps the verified Stage 15 demo initial state; subsequent inputs are live
network inputs, not the demo recording. Original executable stays unchanged.
"""
import argparse
from contextlib import nullcontext
import gzip
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import struct
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from netplay_protocol import Client, ProtocolError, SessionEnded, fingerprint, require
from win32_debug import Debugger

ROOT = Path(__file__).resolve().parent.parent
# 0x422b29/0x422930 build 35 eight-byte command nodes at +0x588.
# Each node is (next node, command data); +0x6a0..+0x6c8 hold six list heads.
# Data pointers can be unaligned and vary between processes, including +0x59c
# when a bow hit uses the third node. Compare active records below instead.
COMMAND_POINTER_FIELDS = tuple(range(0x58c, 0x6a0, 8))
POINTER_FIELDS = (0x64, 0x57c, 0x584, 0x58c, 0x594, 0x5b8, 0x5c4, 0x5d0, 0x5dc, 0x5e8,
                  0x76c, 0x770, 0x774, 0x790, 0x794, 0x798, 0x7f8, 0x7fc, 0x938)
PRESENTATION_FIELDS = (0x2e8, 0x2f4, 0x3f4, 0x8d4, 0x8d8, 0x8dc)


def capture_commands(debugger, node, blob, cache):
    """Compare bounded active command prefixes and their native list order."""
    records, seen = [], set()
    for head in range(0x6a0, 0x6d0, 8):
        current = struct.unpack_from('<I', blob, head)[0]
        while current:
            offset = current - node
            require(0x588 <= offset < 0x6a0 and (offset - 0x588) % 8 == 0,
                    'Unexpected command node layout')
            require(offset not in seen, 'Repeated command node')
            seen.add(offset)
            current, pointer = struct.unpack_from('<2I', blob, offset)
            require(pointer != 0, 'Missing active command data')
            if pointer not in cache:
                cache[pointer] = debugger.read(pointer, 64)
            records.append((head, offset, cache[pointer]))
    return records


class FrameLog:
    """Bound production session logs to three files of at most 8 MiB each."""
    def __init__(self, path, bounded):
        self.handler = RotatingFileHandler(path, maxBytes=8 * 1024 * 1024 if bounded else 0,
                                           backupCount=2, encoding='utf-8')

    def write(self, text):
        self.handler.emit(logging.LogRecord('frames', logging.INFO, '', 0, text.rstrip('\n'), (), None))

    def flush(self):
        self.handler.flush()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.handler.close()


def capture_state(debugger, raw_entities=None):
    """Known gameplay candidates, not a hash of all process memory.

    All active entity bytes except identified address/presentation words;
    includes positions, links, child lists, character records and all RNG streams.
    The two rectangle records referenced at +0x7f8/+0x7fc and 64-byte prefixes
    of active command records are also compared. Complete variable-length
    commands, other pointed-to allocations and unlisted globals are outside it.
    """
    digest = hashlib.sha256()
    characters = debugger.read(0x25109c8, 0x660 * 3)
    rng = debugger.read(0x2558028, 12)
    digest.update(characters)
    digest.update(rng)
    seen = set()
    positions = []
    command_cache = {}

    def capture(node, group, parent=-1):
        require(node not in seen, 'Repeated entity')
        require(0xbd3fc0 <= node < 0x22e84e8 and (node - 0xbd3fc0) % 0x974 == 0, 'Unexpected entity layout')
        seen.add(node)
        blob = debugger.read(node, 0x974)
        slot = (node - 0xbd3fc0) // 0x974
        # 0x448e35 stores at most two pointers to 8-byte rectangle records;
        # 0x44896d passes each to 0x4487c0, which reads four signed shorts.
        # Compare the active records' contents, not process-specific addresses.
        rectangle_count = struct.unpack_from('<I', blob, 0x7f4)[0]
        require(rectangle_count <= 2, 'Unexpected rectangle count')
        rectangles = [debugger.read(struct.unpack_from('<I', blob, 0x7f8 + i * 4)[0], 8)
                      for i in range(rectangle_count)]
        # 0x4229ef stores a command-record pointer at +0x584; 0x423686
        # dereferences it via 0x423620. Compare record bytes, not heap addresses.
        condition_pointer = struct.unpack_from('<I', blob, 0x584)[0]
        condition_prefix = debugger.read(condition_pointer, 64) if condition_pointer else b''
        commands = capture_commands(debugger, node, blob, command_cache)
        if raw_entities is not None:
            raw_entities.append({'group': group, 'slot': slot, 'parent': parent, 'raw': blob.hex(),
                                 'rectangles': [record.hex() for record in rectangles],
                                 'condition_prefix': condition_prefix.hex(),
                                 'commands': [[head, offset, prefix.hex()] for head, offset, prefix in commands]})
        normalized = bytearray(blob)
        for offset in POINTER_FIELDS + PRESENTATION_FIELDS + COMMAND_POINTER_FIELDS:
            normalized[offset:offset + 4] = b'\0' * 4
        digest.update(struct.pack('<IIi', group, slot, parent))
        digest.update(normalized)
        digest.update(bytes([bool(condition_pointer)]) + condition_prefix)
        digest.update(struct.pack('<I', len(commands)))
        for head, offset, prefix in commands:
            digest.update(struct.pack('<II', head, offset) + prefix)
        for record in rectangles:
            digest.update(record)
        positions.append([group, slot, *struct.unpack_from('<3I', blob, 0x98)])
        child = struct.unpack_from('<I', blob, 20)[0]
        while child:
            child_blob = capture(child, group, slot)
            child = struct.unpack_from('<I', child_blob, 12)[0]
        return blob

    for group in range(6):
        node = debugger.uint(0x22e84ec + group * 0x974)
        while node:
            blob = capture(node, group)
            node = struct.unpack_from('<I', blob, 4)[0]
    return {'state': digest.hexdigest(), 'characters': hashlib.sha256(characters).hexdigest(),
            'jobs': [struct.unpack_from('<i', characters, p * 0x660)[0] for p in range(3)],
            'rng': list(struct.unpack('<3I', rng)), 'entities': len(seen),
            'positions': hashlib.sha256(json.dumps(positions, separators=(',', ':')).encode()).hexdigest()}


def scripted_input(recording, frame, slot):
    # Isolated, identical-key test: 1P only, 2P only, 3P only, then all three.
    if 1800 <= frame < 1920:
        phase = (frame - 1800) // 30
        return 0x10 if phase == slot or phase == 3 else 0
    source_frame = frame - 30 * slot
    return recording[source_frame * 3] if source_frame >= 0 and source_frame * 3 < len(recording) else 0


def run_game(directory, client, log_path, scripted=False, seconds=None, perturb_state=False, keyboard_test=False, dump_from=None, stop_after=None, full_log=False):
    directory = Path(directory).resolve()
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    local_input = 0
    loaded = False
    recording = b''
    frame = 0
    started = time.monotonic()
    last_tick = started
    native_mapping = None
    end_reason = 'Frame test completed' if client.frames else 'Game window closed'
    raw_capture = (gzip.open(log_path.with_suffix('.entities.jsonl.gz'), 'wt', encoding='utf-8', compresslevel=1)
                   if dump_from is not None else nullcontext())
    with FrameLog(log_path, bounded=not client.frames and not full_log) as log, raw_capture as raw_log:
        def test_keyboard_buffer(d, ctx):
            # Development-only injection into this owned game's DirectInput
            # sample, before its normal key-to-action mapper. No OS key events.
            # All PCs use scan code 0x2c (Z); pulse one connection at a time.
            keyboard = d.uint(0xbc80d4)
            buffer = d.uint(keyboard + 0x20)
            keys = bytearray(256)
            if loaded and 1800 <= frame < 2280:
                phase = (frame - 1800) // 120
                if phase == client.slot or phase == 3:
                    keys[0x2c] = 0x80
            d.write(buffer, bytes(keys))

        def poll(d, ctx):
            nonlocal local_input, native_mapping
            if loaded:
                require(time.monotonic() - last_tick < 10,
                        'The experimental stage stopped advancing; session ended')
            ptr = d.uint(0xbc80c4)
            require(bool(ptr), 'Input storage unavailable')
            # Every PC reads exactly the same LOCAL 1P mapping. The server owns
            # the global slot assignment. Local 2P/3P mappings are never sent.
            local_input = d.uint(ptr) & 0xff
            if native_mapping is None:
                config = d.uint(0xbc80c0)
                native_mapping = {'kind': d.words(config, 8), 'scan_codes': d.words(config + 0x70, 8)}
                print('LOCAL_1P_MAPPING', json.dumps(native_mapping), flush=True)
            # Prevent physical keys from independently driving local menus,
            # pausing one instance, or cancelling the demo bootstrap.
            d.write(ptr, b'\0' * 12)
            processors = d.uint(0xbc80bc)
            for player in range(3):
                processor = d.uint(processors + player * 4)
                d.write(processor + 4, b'\0' * 4)
            d.write(0xbc80cc, b'\0' * 4)

        def replay_loaded(d, ctx):
            nonlocal loaded, recording, last_tick
            require(not loaded, 'Stage transition is not supported in this experimental build')
            samples = d.uint(0xb064b0)
            require(1 <= samples <= 216000, 'Invalid initial replay length')
            recording = d.read(0xb2968c, samples * 3)
            header = bytearray(d.read(0xb064d0, 0x1344))
            character = bytes(header[28:28 + 0x660])
            for player in range(3):
                if player < client.players:
                    header[28 + player * 0x660:28 + (player + 1) * 0x660] = character
                    header[16 + player * 4:20 + player * 4] = character[:4]
                else:
                    # Both the replay roster and character record use job -1
                    # for an absent player. Preserve the other inactive fields.
                    header[16 + player * 4:20 + player * 4] = b'\xff' * 4
                    header[28 + player * 0x660:32 + player * 0x660] = b'\xff' * 4
            shared = client.ready(bytes(header))
            # This build accepts only the locally verified initial scenario.
            require(shared == bytes(header), 'Initial stage data differs')
            d.write(0xb064d0, shared)
            d.write(0x255802c, struct.pack('<2I', 0x12345678, 0x23456789))
            d.write(0x2558020, struct.pack('<2I', 0x12345678, 0x23456789))
            loaded = True
            last_tick = time.monotonic()
            print('ONLINE_READY', 'player', client.slot + 1, 'players', client.players,
                  'frames', client.frames, 'delay', client.delay, flush=True)

        def tick(d, ctx):
            nonlocal frame, last_tick
            if not loaded:
                return
            if (client.frames and frame == client.frames) or (stop_after and frame == stop_after):
                return False
            require(d.uint(0xadeb2c) == 2 and d.uint(0xadeb28) == (1 if frame else 0), 'Unexpected game tick')
            raw_entities = [] if dump_from is not None and frame >= dump_from else None
            state = capture_state(d, raw_entities) if frame else None
            if raw_entities is not None:
                raw_log.write(json.dumps({'frame': frame, 'entities': raw_entities}, separators=(',', ':')) + '\n')
            if state:
                require(all(job >= 0 for job in state['jobs'][:client.players]) and
                        all(job == -1 for job in state['jobs'][client.players:]),
                        'Game roster does not match the room size')
            digest = state['state'] if state else None
            if perturb_state and frame == 1801:
                digest = 'f' * 64  # deliberate integrity-test mismatch, not gameplay
            value = scripted_input(recording, frame, client.slot) if scripted else local_input
            inputs = client.exchange(value, digest)
            last_tick = time.monotonic()
            # Replay cursor is only used by 0x438e80. Reuse the first three
            # bytes each tick, so live play never runs beyond the replay array.
            # Network frame numbers remain monotonic and independent of it.
            d.write(0xadeb28, b'\0' * 4)
            d.write(0xb2968c, bytes(inputs))
            row = {'frame': frame, 'slot': client.slot, 'local_input': value,
                   'native_local_input': local_input, 'inputs': inputs, 'state': state}
            log.write(json.dumps(row, separators=(',', ':')) + '\n')
            frame += 1
            if frame == 1 or frame % 300 == 0:
                log.flush()
                print('NETWORK_FRAMES', frame, 'player', client.slot + 1,
                      'elapsed', round(time.monotonic() - started, 2), flush=True)

        def created(d):
            require(d.base == 0x400000, 'Unexpected image base')
            require(d.read(0x427900, 6) == bytes.fromhex('8b4424048b4c'), 'Unexpected notice function')
            d.write(0x427900, bytes.fromhex('b801000000c3'))
            # The attract-mode timer starts at 10,800 and exits the demo.
            # Disable only that expiry result; gameplay/end-state logic stays.
            require(d.read(0x438fb0, 5) == bytes.fromhex('a1d07bbc00'), 'Unexpected demo timer function')
            d.write(0x438fb0, bytes.fromhex('31c0c3'))
            # The polling return is before edge processing; preserve its 1P
            # result and clear local-only consumers before the next game tick.
            require(d.read(0x43d4cc, 1) == b'\xc3', 'Unexpected polling function')
            d.breakpoint(0x43d4cc, poll)
            if keyboard_test:
                d.breakpoint(0x43d369, test_keyboard_buffer)
            d.breakpoint(0x438e12, replay_loaded)
            d.breakpoint(0x438e80, tick)
            print('GAME_BRIDGE_READY', d.pid, flush=True)

        try:
            Debugger(directory / 'rbo_ex3.exe').run(created, seconds)
        except SessionEnded as exc:
            require(not client.frames, 'Finite test ended unexpectedly')
            end_reason = str(exc)
        else:
            if client.frames:
                require(frame == client.frames, f'Game stopped early: {frame}/{client.frames}')
            elif stop_after and frame == stop_after:
                end_reason = 'Developer stop point reached'
            elif seconds is not None and time.monotonic() - started >= seconds:
                raise ProtocolError('Developer test deadline exceeded')
            client.finish()
    return {'slot': client.slot, 'players': client.players, 'frames': frame, 'elapsed': round(time.monotonic() - started, 2),
            'mapping': native_mapping, 'log': str(log_path), 'unlimited': client.frames == 0, 'end_reason': end_reason}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--game', type=Path, required=True)
    parser.add_argument('--server', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=47624)
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--host', action='store_true')
    parser.add_argument('--scripted', action='store_true')
    parser.add_argument('--perturb-state', action='store_true')
    parser.add_argument('--keyboard-test', action='store_true')
    parser.add_argument('--dump-from', type=int, help='Developer capture: save raw entity records from this frame')
    parser.add_argument('--stop-after', type=int, help='Developer only: close an unlimited game after this many frames')
    parser.add_argument('--full-log', action='store_true', help='Developer only: retain the entire test log')
    parser.add_argument('--seconds', type=int, default=600)
    parser.add_argument('--log', type=Path, required=True)
    args = parser.parse_args()
    game_hash = fingerprint(args.game)
    token = args.token_file.read_text(encoding='utf-8').strip()
    client = Client((args.server, args.port), token, game_hash, 'host' if args.host else 'guest')
    try:
        print('ASSIGNED_PLAYER', client.slot + 1, flush=True)
        result = run_game(args.game, client, args.log, args.scripted, args.seconds, args.perturb_state, args.keyboard_test, args.dump_from, args.stop_after, args.full_log)
        print('GAME_SESSION_COMPLETE', json.dumps(result), flush=True)
    finally:
        client.close()


if __name__ == '__main__':
    main()
