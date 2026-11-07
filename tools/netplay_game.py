"""Experimental EX3 game bridge: local 1P mapping -> connection-owned player slot.

Bootstraps the verified Stage 15 demo initial state; subsequent inputs are live
network inputs, not the demo recording. Original executable stays unchanged.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from netplay_protocol import Client, ProtocolError, fingerprint, require
from win32_debug import Debugger

ROOT = Path(__file__).resolve().parent.parent
POINTER_FIELDS = (0x64, 0x57c, 0x58c, 0x5b8, 0x5c4, 0x5d0, 0x5dc, 0x5e8,
                  0x76c, 0x770, 0x774, 0x790, 0x794, 0x798, 0x938)
PRESENTATION_FIELDS = (0x2e8, 0x2f4, 0x3f4, 0x8d4, 0x8d8, 0x8dc)


def capture_state(debugger):
    """Known gameplay candidates, not a hash of all process memory.

    All active entity bytes except identified address/presentation words;
    includes positions, links, child lists, character records and all RNG streams.
    Pointed-to allocations and unlisted globals are outside this check.
    """
    digest = hashlib.sha256()
    characters = debugger.read(0x25109c8, 0x660 * 3)
    rng = debugger.read(0x2558028, 12)
    digest.update(characters)
    digest.update(rng)
    seen = set()
    positions = []

    def capture(node, group, parent=-1):
        require(node not in seen, 'Repeated entity')
        require(0xbd3fc0 <= node < 0x22e84e8 and (node - 0xbd3fc0) % 0x974 == 0, 'Unexpected entity layout')
        seen.add(node)
        blob = debugger.read(node, 0x974)
        slot = (node - 0xbd3fc0) // 0x974
        normalized = bytearray(blob)
        for offset in POINTER_FIELDS + PRESENTATION_FIELDS:
            normalized[offset:offset + 4] = b'\0' * 4
        digest.update(struct.pack('<IIi', group, slot, parent))
        digest.update(normalized)
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


def run_game(directory, client, log_path, scripted=False, seconds=600, perturb_state=False, keyboard_test=False):
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
    with log_path.open('w', encoding='utf-8') as log:
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
            require(1 <= samples <= 216000 and client.frames + client.delay < samples,
                    'Session exceeds the verified stage duration')
            recording = d.read(0xb2968c, samples * 3)
            header = bytearray(d.read(0xb064d0, 0x1344))
            character = bytes(header[28:28 + 0x660])
            for player in range(3):
                header[28 + player * 0x660:28 + (player + 1) * 0x660] = character
                header[16 + player * 4:20 + player * 4] = character[:4]
            shared = client.ready(bytes(header))
            # This build accepts only the locally verified initial scenario.
            require(shared == bytes(header), 'Initial stage data differs')
            d.write(0xb064d0, shared)
            d.write(0x255802c, struct.pack('<2I', 0x12345678, 0x23456789))
            d.write(0x2558020, struct.pack('<2I', 0x12345678, 0x23456789))
            loaded = True
            last_tick = time.monotonic()
            print('ONLINE_READY', 'player', client.slot + 1, 'frames', client.frames, 'delay', client.delay, flush=True)

        def tick(d, ctx):
            nonlocal frame, last_tick
            if not loaded:
                return
            if frame == client.frames:
                return False
            require(d.uint(0xadeb2c) == 2 and d.uint(0xadeb28) == frame, 'Unexpected game tick')
            state = capture_state(d) if frame else None
            digest = state['state'] if state else None
            if perturb_state and frame == 1801:
                digest = 'f' * 64  # deliberate integrity-test mismatch, not gameplay
            value = scripted_input(recording, frame, client.slot) if scripted else local_input
            inputs = client.exchange(value, digest)
            last_tick = time.monotonic()
            # The existing replay input gate supplies all THREE network values
            # before any player's simulation runs. It also covers setup tick 0.
            d.write(0xb2968c + frame * 3, bytes(inputs))
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
            # The polling return is before edge processing; preserve its 1P
            # result and clear local-only consumers before the next game tick.
            require(d.read(0x43d4cc, 1) == b'\xc3', 'Unexpected polling function')
            d.breakpoint(0x43d4cc, poll)
            if keyboard_test:
                d.breakpoint(0x43d369, test_keyboard_buffer)
            d.breakpoint(0x438e12, replay_loaded)
            d.breakpoint(0x438e80, tick)
            print('GAME_BRIDGE_READY', d.pid, flush=True)

        Debugger(directory / 'rbo_ex3.exe').run(created, seconds)
    require(frame == client.frames, f'Game stopped early: {frame}/{client.frames}')
    client.finish()
    return {'slot': client.slot, 'frames': frame, 'elapsed': round(time.monotonic() - started, 2),
            'mapping': native_mapping, 'log': str(log_path)}


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
    parser.add_argument('--seconds', type=int, default=600)
    parser.add_argument('--log', type=Path, required=True)
    args = parser.parse_args()
    game_hash = fingerprint(args.game)
    token = args.token_file.read_text(encoding='utf-8').strip()
    client = Client((args.server, args.port), token, game_hash, 'host' if args.host else 'guest')
    try:
        print('ASSIGNED_PLAYER', client.slot + 1, flush=True)
        result = run_game(args.game, client, args.log, args.scripted, args.seconds, args.perturb_state, args.keyboard_test)
        print('GAME_SESSION_COMPLETE', json.dumps(result), flush=True)
    finally:
        client.close()


if __name__ == '__main__':
    main()
