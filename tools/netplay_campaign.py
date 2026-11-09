"""Native normal-mode bridge, synchronized before menu and gameplay updates."""
import hashlib
import json
from pathlib import Path
import struct
import time
from collections import deque

from netplay_game import FrameLog, capture_state
from netplay_protocol import require, ProtocolError, SessionEnded, GameCrashed
from win32_debug import Debugger
from netplay_saves import capture as capture_saves, encode
from netplay_stage import capture_stage, restore_stage

TICK_HZ = 120
DEFAULT_KEYS = (35, 49, 48, 50, 44, 45, 46, 47, 16)


def patch(d, address, expected, replacement):
    require(d.read(address, len(expected)) == expected, f'Unsupported code at {address:08x}')
    d.write(address, replacement)


def run_campaign(directory, client, log_path, *, seconds=None, control=None, full_log=False, checkpoint=None, save_store=None, stage=None, stage_observer=None):
    directory, log_path = Path(directory), Path(log_path)
    frame = 0
    last_scene = None
    next_tick = time.monotonic()
    mapping = None
    result = {'frames': 0, 'slot': client.slot, 'players': client.players}
    published = checkpoint
    committed = checkpoint
    save_dirty = False
    clock_data = None
    recent = deque(maxlen=client.delay + 8)
    stage_pending = None
    restore_pending = stage is not None
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with FrameLog(log_path, bounded=not full_log) as log:
        def record_failure(error, meta, raw, d=None):
            diagnostic = {'frame': frame, 'state': meta, 'entities': raw, 'recent': list(recent), 'error': str(error)}
            try:
                if d is not None:
                    diagnostic['characters'] = d.read(0x25109c8, 0x660 * 3).hex()
            except OSError:
                pass
            try:
                log_path.with_suffix('.failure.json').write_text(json.dumps(diagnostic), encoding='utf-8')
            except OSError:
                pass
            if hasattr(client, 'report_failure'):
                client.report_failure(diagnostic)

        def seed(d, ctx):
            index = d.uint(ctx.esp + 4)
            require(index < 3, 'Invalid random stream')
            d.write(ctx.esp + 8, struct.pack('<I', (0x12345678 + index * 0x11111111) & 0x7fffffff))

        def poll(d, ctx):
            nonlocal frame, last_scene, next_tick, mapping, published, committed, save_dirty, stage_pending
            if client.frames and frame == client.frames:
                return False
            d.write(clock_data, struct.pack('<QI', frame * 10000, frame * 1000 // TICK_HZ & 0xffffffff))
            config = d.uint(0xbc80c0)
            if mapping is None:
                kinds = d.words(config, 9)
                keys = d.words(config + 0x70, 9)
                # Online keyboard default; preserve existing keyboard bindings.
                for i in range(9):
                    if kinds[i] != 1 or not 0 < keys[i] < 256:
                        d.write(config + i * 4, struct.pack('<I', 1))
                        d.write(config + 0x70 + i * 4, struct.pack('<I', DEFAULT_KEYS[i]))
                mapping = {'kind': d.words(config, 9), 'scan_codes': d.words(config + 0x70, 9)}
                print('LOCAL_1P_MAPPING', json.dumps(mapping), flush=True)
            ptr = d.uint(0xbc80c4)
            native = d.uint(ptr) & 0x1ff
            scene = d.uint(0xbc91a0)
            # The shared menus are always driven by 1P. Character creation
            # retains the game's independent per-player selectors.
            common_menu = scene != 4 or d.uint(0x4a5e78) >= 2
            if common_menu:
                d.write(0xade9d0, b'\0' * 4)
            meta = {'scene': scene, 'next_scene': d.uint(0xbc91a4),
                    'mode': d.uint(0xadeb2c), 'stage': d.uint(0xb07948),
                    'rng': d.words(0x2558028, 3),
                    'characters': hashlib.sha256(d.read(0x25109c8, 0x660 * 3)).hexdigest(),
                    'jobs': [struct.unpack('<i', d.read(0x25109c8 + i * 0x660, 4))[0] for i in range(3)]}
            if scene != 0:
                meta['fade'] = d.words(0x255fa28, 10)
            if scene == 4:
                meta['selection'] = [[d.uint(0x4a2920 + i * 0x1018 + off) for off in (0x44, 0x4c, 0xc)] for i in range(3)]
                meta['party'] = d.words(0x4a6490, 4)
                meta['party_phase'] = d.uint(0x4a5e78)
            if scene != last_scene:
                print('SCENE', frame, meta, flush=True)
                last_scene = scene
            # Development control is supplied only by the local test harness.
            local = control(frame, client.slot, meta, d) if control else native
            if client.slot == 0 and control is None:
                local |= (d.uint(0xbc80cc) & 0x1800) << 9
            raw = []
            if scene == 0:
                meta['pause'] = d.words(0xbc7af8, 4)
                meta['battle_phase'] = d.uint(0xadff7c)
                try:
                    meta['combat'] = capture_state(d, raw)
                except (ProtocolError, OSError) as error:
                    record_failure(error, meta, raw, d)
                    raise GameCrashed('게임 상태를 안전하게 읽지 못해 시작 지점에서 복구합니다.') from error
            if stage_pending is not None and frame:
                meta['stage_checkpoint'] = hashlib.sha256(encode(stage_pending)).hexdigest()
            digest = hashlib.sha256(json.dumps(meta, sort_keys=True).encode()).hexdigest() if frame else None
            recent.append({'frame': frame, 'state': meta, 'entities': raw})
            update = None
            if frame and client.slot == 0 and published is not None and (save_dirty or frame % 120 == 0):
                current = capture_saves(directory / 'DATA/SaveData', published['profile'], published['revision'] + 1)
                if current['files'] != published['files']:
                    update = current
                    published = current
                save_dirty = False
            try:
                extra = {'stage': stage_pending} if frame and client.slot == 0 and getattr(client, 'supports_recovery', False) else {}
                inputs = client.exchange(local, digest, checkpoint=update, detail=meta, **extra)
                if frame:
                    stage_pending = None
            except SessionEnded:
                raise
            except ProtocolError as error:
                record_failure(error, meta, raw, d)
                raise
            accepted = client.received_checkpoint
            if accepted is not None and accepted != committed:
                require(checkpoint is not None and accepted['profile'] == checkpoint['profile'], 'Unexpected save identity')
                if save_store:
                    save_store.commit(accepted)
                committed = accepted
                print('SAVE_CONFIRMED', accepted['revision'], 'characters', sum(name.startswith('chr_data') for name in accepted['files']), flush=True)
            # Native edge processing remains in the game, after this hook.
            native_inputs = [value & 0x1ff for value in inputs]
            if scene != 0 and common_menu:
                native_inputs[1:] = [0, 0]
            if scene == 0:
                native_inputs[1:] = [value & 0xff for value in native_inputs[1:]]
            d.write(ptr, struct.pack('<3I', *native_inputs))
            processors = d.uint(0xbc80bc)
            for i, value in enumerate(native_inputs):
                d.write(d.uint(processors + i * 4) + 4, struct.pack('<I', value))
            d.write(0xbc80cc, struct.pack('<I', (inputs[0] >> 9) & 0x1800))
            log.write(json.dumps({'frame': frame, 'slot': client.slot, 'local_input': local,
                                  'inputs': inputs, 'state': meta}, separators=(',', ':')) + '\n')
            frame += 1
            if frame % 240 == 0:
                print('NETWORK_FRAMES', frame, 'scene', scene, flush=True)
            # Do not catch up after loading stalls: one shared update per tick.
            next_tick = max(next_tick + 1 / TICK_HZ, time.monotonic())
            time.sleep(max(0, next_tick - time.monotonic()))

        def created(d):
            nonlocal clock_data
            require(d.base == 0x400000, 'Unsupported image base')
            from netplay_korean import attach
            attach(d, directory)
            patch(d, 0x427900, bytes.fromhex('8b4424048b4c'), bytes.fromhex('b801000000c3'))
            # Virtualize the game's timer wrappers, not Windows or other apps.
            # 1,200,000 ticks/sec gives exact 120 Hz updates and 60 Hz drawing.
            clock_data = d.allocate_data(16)
            d.write(clock_data, bytes(16))
            patch(d, 0x46a610, bytes.fromhex('ff2574624800'), b'\xa1' + struct.pack('<I', clock_data + 8) + b'\xc3')
            patch(d, 0x46a620, bytes.fromhex('6808805502'), b'\xa1' + struct.pack('<I', clock_data) + b'\x8b\x15' + struct.pack('<I', clock_data + 4) + b'\xc3')
            def frequency(d, ctx):
                d.write(ctx.esp, struct.pack('<Q', 1200000))
                ctx.eax = 1
            d.breakpoint(0x46a69e, frequency)
            # Recovery first initializes the native bar's per-player menu
            # objects; a direct battle boot leaves them null after a clear.
            d.write(0x497578, struct.pack('<I', 4 if stage else 2))
            def recovery_boot(d, ctx):
                if restore_pending and d.uint(0xbc91a0) == 4:
                    d.remove_breakpoint(0x43fab0)
                    d.write(0xbc91a4, bytes(4))
                    ctx.eax = 1
                    ctx.eip = d.uint(ctx.esp)
                    ctx.esp += 4
            if stage:
                d.breakpoint(0x43fab0, recovery_boot)
            # Stay at the online title menu instead of entering attract mode.
            patch(d, 0x477380, bytes.fromhex('e8'), bytes.fromhex('b801000000c3'))
            # Title transition already waits for its frame-counted fade. Do not
            # additionally wait for two local DirectSound buffers to finish.
            patch(d, 0x476fe0, bytes.fromhex('6a01e8f9f8fe'), bytes.fromhex('b801000000c3'))
            # The bar, map and result menus have the same sound-only barriers;
            # their existing frame-counted fades and input edges stay intact.
            patch(d, 0x406960, bytes.fromhex('5633f656'), bytes.fromhex('b801000000c3'))
            patch(d, 0x4069b0, bytes.fromhex('6a01e829ff05'), bytes.fromhex('b801000000c3'))
            patch(d, 0x457790, bytes.fromhex('6a01e849f100'), bytes.fromhex('b801000000c3'))
            # Native BGM completion also gates bar/result/map transitions. A
            # muted peer reports completion immediately, unlike a playing peer.
            # Keep the surrounding frame-counted fade/timer checks, bypass only
            # these menu-local audio queries; leave the audio engine untouched.
            for address in (0x4079d5, 0x4079e3, 0x40ab5d, 0x40ab6b,
                            0x40abce, 0x40abe0, 0x4682b5, 0x4682e7):
                expected = b'\xe8' + struct.pack('<i', 0x4666e0 - address - 5)
                patch(d, address, expected, bytes.fromhex('31c0909090'))
            # Result-to-bar transition already waits for its visual fade and a
            # fixed timer. Its additional volume-envelope wait is local audio.
            patch(d, 0x458600, bytes.fromhex('56be14000000'), bytes.fromhex('b801000000c3'))
            # Normal-mode replay recording is separate from character saves.
            # Skip its finite input buffer and 25/30-minute warning popups.
            patch(d, 0x438edf, bytes.fromhex('8b0db064b000'), bytes.fromhex('e98700000090'))
            # WM_NCACTIVATE / WM_CANCELMODE flush pending battle photos on only
            # the locally activated/deactivated PC. That flush consumes RNG 1
            # and changes photo slots, desynchronizing otherwise equal games.
            # Defer it to the existing shared scene teardown. Keep Windows'
            # default activation handling and every RNG/state comparison.
            patch(d, 0x43f37a, bytes.fromhex('e881adffff'), b'\x90' * 5)
            def map_start(d, ctx):
                if d.uint(0x2553c20) == 0xffffffff:
                    d.write(0x2553c20, b'\0' * 4)
            d.breakpoint(0x467e70, map_start)
            def stage_start(d, ctx):
                nonlocal restore_pending, stage_pending
                if restore_pending:
                    restore_stage(d, stage, client.players)
                    restore_pending = False
                if d.uint(0xadeb2c) == 1 and d.uint(0xb064b4) == 0:
                    stage_pending = capture_stage(d, client.players)
                    if stage_observer:
                        stage_observer(stage_pending)
            d.breakpoint(0x43bd00, stage_start)
            # Native local play accepts any number of joined characters. An
            # online room must wait for every assigned player before selection.
            def party_ready(d, ctx):
                if ctx.eax != client.players:
                    ctx.eax = 0
            d.breakpoint(0x407ad6, party_ready)
            def character_saved(d, ctx):
                nonlocal save_dirty
                require(ctx.eax == 1, 'Native character save failed')
                save_dirty = True
            d.breakpoint(0x456c00, character_saved)
            d.breakpoint(0x46a4b0, seed)
            d.breakpoint(0x43d4cc, poll)
            print('CAMPAIGN_READY', d.pid, flush=True)

        try:
            debugger = Debugger(directory / 'rbo_ex3.exe')
            debugger.run(created, seconds)
            if debugger.exit_code not in (None, 0):
                error = GameCrashed(f'Game process exited with code 0x{debugger.exit_code:08x}; native exceptions: {debugger.exceptions}')
                latest = recent[-1] if recent else {}
                record_failure(error, latest.get('state', {}), latest.get('entities', []))
                raise error
            if client.frames:
                require(frame == client.frames, f'Game stopped early: {frame}/{client.frames}')
            client.finish()
        except SessionEnded as exc:
            result['end_reason'] = str(exc)
        finally:
            accepted = client.received_checkpoint
            if accepted is not None and save_store is not None:
                require(checkpoint is not None and accepted['profile'] == checkpoint['profile'], 'Unexpected save identity')
                save_store.commit(accepted)
                result['save_revision'] = accepted['revision']
    result.update(frames=frame, mapping=mapping, transport_resumes=getattr(client, 'transport_resumes', 0))
    return result
