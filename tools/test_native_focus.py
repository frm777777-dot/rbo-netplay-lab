"""Silent regression using real native focus callbacks, not desktop key presses.

Creates a pending photo through the native capture function on every peer, then
calls WM_NCACTIVATE/WM_CANCELMODE on only one peer at a time. The unpatched
control must reproduce RNG-only divergence; the fix must retain all state
checks, reach the next stage, and preserve all three character saves.
"""
import argparse
import base64
import ctypes
import json
from pathlib import Path
import secrets
import struct
import subprocess
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
import test_support
from netplay_launcher import isolated_game
from netplay_campaign import run_campaign
from netplay_protocol import fingerprint, ProtocolError
from netplay_transport import RecoveryRoom, RecoveryServer, RecoveryClient
from netplay_saves import SaveStore, materialize, encode
from win32_debug import Debugger, Context


def child(args):
    output = args.output
    directory = isolated_game(args.game)
    store = SaveStore(output / f'saves-{args.slot}')
    initial = json.loads(args.restore.read_bytes())
    client = RecoveryClient(('127.0.0.1', args.port), args.token, fingerprint(args.game),
                            'host' if args.slot == 0 else 'guest', timeout=45)
    checkpoint = json.loads(client.ready(encode(initial) if client.slot == 0 else None))
    store.commit(checkpoint)
    materialize(checkpoint, directory / 'DATA/SaveData')
    callbacks = []
    pending = None
    battle = 0

    class FocusDebugger(Debugger):
        def run(self, on_create, seconds=None):
            def created(d):
                on_create(d)
                if args.unpatched:
                    assert d.read(0x43f37a, 5) == b'\x90' * 5
                    d.write(0x43f37a, bytes.fromhex('e881adffff'))
                # The byte immediately after the input routine's RET is padding.
                assert d.read(0x43d4cd, 1) == b'\x90'
                d.breakpoint(0x43d4cd, returned)
            super().run(created, seconds)

        def breakpoint(self, address, callback):
            if address != 0x43d4cc:
                return super().breakpoint(address, callback)

            def poll(d, context):
                nonlocal pending
                keep = callback(d, context)
                if keep is False or pending is None:
                    return keep
                kind, wparam, frame = pending
                pending = None
                d.focus_context = Context.from_buffer_copy(bytes(context))
                d.focus_record = {'frame': frame, 'kind': kind, 'wparam': wparam,
                                  'rng_before': d.words(0x2558028, 3),
                                  'photos_before': d.words(0xadd9a4, 3)}
                if kind == 'capture':
                    target, arguments = 0x43a390, []
                else:
                    assert 1 in d.focus_record['photos_before'], 'No pending photo'
                    message = {'activate': 0x86, 'cancel': 0x1f}[kind]
                    target, arguments = 0x43f290, [d.uint(0x256c098), message, wparam, 0]
                # Execute the original cdecl routine on its own game thread.
                # Restore all registers/flags and emulate the interrupted RET.
                values = [0x43d4cd, *arguments]
                context.esp -= 4 * len(values)
                d.write(context.esp, struct.pack('<' + 'I' * len(values), *values))
                context.eip = target
                return keep
            return super().breakpoint(address, poll)

    def returned(d, context):
        record = d.focus_record
        record.update(rng_after=d.words(0x2558028, 3), photos_after=d.words(0xadd9a4, 3),
                      native_return=context.eax)
        callbacks.append(record)
        print('NATIVE_FOCUS_CALLBACK', json.dumps(record), flush=True)
        if record['kind'] == 'capture':
            assert 1 in record['photos_after']
        elif not args.unpatched:
            assert record['rng_before'] == record['rng_after'], record
            assert record['photos_before'] == record['photos_after'], record
            assert context.eax == 2, 'Default activation handling must be retained'
        original = d.focus_context
        ctypes.memmove(ctypes.byref(context), ctypes.byref(original), ctypes.sizeof(Context))
        context.eip = d.uint(original.esp)
        context.esp = original.esp + 4

    def control(frame, slot, meta, d):
        nonlocal pending, battle
        if meta['scene'] == 0:
            battle += 1
            if battle == 240:
                pending = ('capture', 0, frame)
            schedule = {360: (client.players - 1, 'activate', 0),
                        390: (client.players - 1, 'activate', 1),
                        420: (client.players - 1, 'cancel', 0),
                        480: (0, 'activate', 0), 510: (0, 'activate', 1),
                        540: (0, 'cancel', 0), 600: (1, 'activate', 0),
                        630: (1, 'activate', 1), 660: (1, 'cancel', 0)}
            event = schedule.get(battle)
            if event and event[0] == slot:
                pending = (event[1], event[2], frame)
            if battle == 850 and not args.unpatched:
                d.write(0xadff7c, struct.pack('<I', 7))
        return 16 if frame % 60 < 15 else 0

    result = {}
    try:
        with patch('netplay_campaign.Debugger', FocusDebugger):
            result = run_campaign(directory, client, output / f'{args.slot}.jsonl', seconds=180,
                                  control=control, full_log=True, checkpoint=checkpoint,
                                  save_store=store, stage=client.stage)
    except ProtocolError as error:
        result['error'] = str(error)
        if not args.unpatched:
            raise
    finally:
        result['callbacks'] = callbacks
        (output / f'{args.slot}.result.json').write_text(json.dumps(result, indent=2))
        client.close()


def parent(args):
    output = ROOT / 'analysis' / args.tag
    output.mkdir(exist_ok=False)
    room = RecoveryRoom(secrets.token_hex(16), secrets.token_hex(16), frames=args.frames,
                        players=args.players, timeout=45, diagnostics_dir=output / 'events')
    room.stage = json.loads(args.stage.read_bytes())
    if args.players == 2:
        data = bytearray(base64.b64decode(room.stage['characters']))
        struct.pack_into('<i', data, 2 * 0x660, -1)
        room.stage['characters'] = base64.b64encode(data).decode()
    server = RecoveryServer(room, ('127.0.0.1', 0))
    thread = threading.Thread(target=server.serve)
    thread.start()
    processes, logs = [], []
    started = time.monotonic()
    try:
        for slot in range(args.players):
            log = (output / f'{slot}.txt').open('w', encoding='utf-8')
            logs.append(log)
            command = [sys.executable, '-B', '-X', 'utf8', str(Path(__file__).resolve()),
                       '--slot', str(slot), '--output', str(output), '--port', str(server.address[1]),
                       '--token', room.host_token if slot == 0 else room.token,
                       '--game', str(args.game), '--restore', str(args.restore)]
            if args.unpatched:
                command.append('--unpatched')
            processes.append(subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                              creationflags=subprocess.CREATE_NO_WINDOW))
            deadline = time.monotonic() + 20
            while slot not in room.members and time.monotonic() < deadline:
                time.sleep(.1)
            assert slot in room.members
        print('NATIVE_FOCUS_STARTED', args.players, 'unpatched', args.unpatched, flush=True)
        for process in processes:
            process.wait(timeout=max(1, 210 - (time.monotonic() - started)))
        results = [json.loads((output / f'{slot}.result.json').read_bytes()) for slot in range(args.players)]
        rows = [[json.loads(line) for line in (output / f'{slot}.jsonl').read_text().splitlines()]
                for slot in range(args.players)]
        saves = [json.loads((output / f'saves-{slot}/current.json').read_bytes()) for slot in range(args.players)]
        callbacks = [event for result in results for event in result['callbacks'] if event['kind'] != 'capture']
        summary = {'exit_codes': [p.returncode for p in processes], 'recovery': list(room.recovery_history),
                   'error': room.error, 'frames': list(map(len, rows)), 'unpatched': args.unpatched,
                   'focus_callbacks': len(callbacks),
                   'callback_rng_changes': sum(e['rng_before'] != e['rng_after'] for e in callbacks),
                   'stages': sorted({r['state']['stage'] + 1 for r in rows[0] if r['state']['scene'] == 0}),
                   'identical': all(row == [{**r, 'slot': i} for r in rows[0]] for i, row in enumerate(rows)),
                   'saves_identical': all(save == saves[0] for save in saves),
                   'save_files': sorted(saves[0]['files']), 'elapsed': round(time.monotonic() - started, 2)}
        (output / 'summary.json').write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary), flush=True)
        assert summary['exit_codes'] == [0] * args.players and room.error is None, summary
        assert summary['saves_identical'], summary
        original = json.loads(args.restore.read_bytes())
        assert set(original['files']) <= set(saves[0]['files']), 'Lost saved characters'
        if args.unpatched:
            assert room.epoch == 1 and 'diverged at frame' in room.recovery_history[0]['reason'], summary
            assert summary['callback_rng_changes'] == 1, summary
            event = callbacks[0]
            assert event['rng_before'][::2] == event['rng_after'][::2], event
        else:
            assert room.epoch == 0 and summary['frames'] == [args.frames] * args.players, summary
            assert summary['identical'] and summary['focus_callbacks'] == 9, summary
            assert summary['callback_rng_changes'] == 0 and summary['stages'] == [1, 2], summary
        if args.players == 2:
            assert original['files']['chr_data03.sv4'] == saves[0]['files']['chr_data03.sv4']
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(10)
        for log in logs:
            log.close()
        server.close()
        thread.join(3)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--slot', type=int)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--port', type=int)
    parser.add_argument('--token')
    parser.add_argument('--game', type=Path, default=ROOT / 'runtime/app')
    parser.add_argument('--restore', type=Path, default=ROOT / 'analysis/campaign-save-1/saves-0/current.json')
    parser.add_argument('--stage', type=Path, default=ROOT / 'analysis/native-stage-restore-3/0.stage-0.json')
    parser.add_argument('--tag', default='native-focus')
    parser.add_argument('--players', type=int, choices=(2, 3), default=3)
    parser.add_argument('--frames', type=int, default=2500)
    parser.add_argument('--unpatched', action='store_true')
    options = parser.parse_args()
    child(options) if options.slot is not None else parent(options)
