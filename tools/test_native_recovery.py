"""Owned-game integration test with a real dropped socket/desync/native exit."""
import argparse
import base64
import json
from pathlib import Path
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
import test_support  # Enforce silence in every owned game child.
from netplay_launcher import isolated_game
from netplay_campaign import run_campaign
from netplay_protocol import fingerprint, RecoveryNeeded, TransportError, GameCrashed, PreparedRestart
from netplay_transport import RecoveryRoom, RecoveryServer, RecoveryClient
from netplay_saves import SaveStore, materialize, encode
from win32_debug import Terminate


def child(args):
    output = Path(args.output)
    store = SaveStore(output / f'saves-{args.index}')
    initial = json.loads(args.restore.read_bytes())
    client = RecoveryClient(('127.0.0.1', args.port), args.token, fingerprint(args.game),
                            'host' if args.index == 0 else 'guest', timeout=45)
    checkpoint = json.loads(client.ready(encode(initial) if client.slot == 0 else None))
    store.commit(checkpoint)
    process_ids = set()
    window_handles = set()
    try:
        while True:
            generation = output / f'epoch-{client.epoch}'
            generation.mkdir(exist_ok=True)
            directory = isolated_game(args.game)
            materialize(checkpoint, directory / 'DATA/SaveData')
            battle_frames = 0
            cleared = False
            seen_epoch = client.epoch

            def control(frame, slot, meta, debugger):
                nonlocal battle_frames, cleared, seen_epoch
                if seen_epoch != client.epoch:
                    battle_frames = 0
                    cleared = False
                    seen_epoch = client.epoch
                process_ids.add(debugger.pid)
                window_handles.add(debugger.uint(0x256c098))
                assert struct.unpack('<20I', (directory / 'DATA/SaveData/SystemConfig.v4').read_bytes())[15] == 3
                if (client.epoch == 0 or args.fault == 'repeat' and client.epoch == 1) and (frame == 360 or args.fault == 'disconnect' and frame in (900, 1200)) and slot == client.players - 1:
                    print('INJECT_FAULT', args.fault, flush=True)
                    if args.fault == 'disconnect':
                        client.socket.shutdown(socket.SHUT_RDWR)
                        client.socket.close()
                    elif args.fault in ('desync', 'repeat', 'reloadfail'):
                        debugger.write(0x25109c8 + 32, struct.pack('<I', 98765))
                        if args.fault == 'reloadfail':
                            original = debugger.watchdog
                            def fail_reload():
                                original()
                                if client.epoch == 1:
                                    raise GameCrashed('Injected scene reload failure')
                            debugger.watchdog = fail_reload
                    elif args.fault == 'crash':
                        Terminate(debugger.handle, 0xdead)
                    else:
                        # An actual native CPU fault, not just TerminateProcess.
                        # This is the owned game's next input-processing routine.
                        debugger.write(0x43da00, b'\x0f\x0b')
                if meta['scene'] == 0:
                    battle_frames += 1
                    if (client.epoch or args.fault == 'disconnect') and not cleared and battle_frames == 650:
                        debugger.write(0xadff7c, struct.pack('<I', 7))
                        cleared = True
                return 16 if frame % 60 < 15 else 0

            try:
                result = run_campaign(directory, client, generation / f'{args.index}.jsonl', seconds=300,
                                      control=control, full_log=True, checkpoint=checkpoint,
                                      save_store=store, stage=client.stage)
                result.update(process_ids=sorted(process_ids), window_handles=sorted(window_handles), transport_resumes=client.transport_resumes)
                (output / f'{args.index}.result.json').write_text(json.dumps(result))
                break
            except (RecoveryNeeded, TransportError, GameCrashed) as error:
                print('RECOVERY', client.epoch, str(error), flush=True)
                if isinstance(error, PreparedRestart):
                    checkpoint = client.received_checkpoint
                else:
                    client.request_recovery()
                    checkpoint = json.loads(client.reconnect(encode(initial) if client.slot == 0 else None))
                store.commit(checkpoint)
                assert client.slot == args.index, 'Seat changed'
    finally:
        client.close()


def parent(args):
    output = ROOT / 'analysis' / args.tag
    output.mkdir(exist_ok=False)
    room = RecoveryRoom(secrets.token_hex(16), secrets.token_hex(16), frames=args.frames, players=args.players, timeout=45,
                        diagnostics_dir=output / 'events')
    room.stage = json.loads(args.stage.read_bytes())
    if args.players == 2:
        blob = bytearray(base64.b64decode(room.stage['characters']))
        struct.pack_into('<i', blob, 2 * 0x660, -1)
        room.stage['characters'] = base64.b64encode(blob).decode()
    server = RecoveryServer(room, ('127.0.0.1', 0))
    thread = threading.Thread(target=server.serve)
    thread.start()
    processes, logs = [], []
    started = time.monotonic()
    try:
        # Connect guests sequentially in the harness to keep file index = seat.
        for i in range(args.players):
            log = (output / f'{i}.txt').open('w', encoding='utf-8')
            logs.append(log)
            command = [sys.executable, '-B', '-X', 'utf8', str(Path(__file__).resolve()), '--index', str(i),
                       '--output', str(output), '--port', str(server.address[1]),
                       '--token', room.host_token if i == 0 else room.token, '--game', str(args.game),
                       '--restore', str(args.restore), '--fault', args.fault]
            processes.append(subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                              creationflags=subprocess.CREATE_NO_WINDOW))
            deadline = time.monotonic() + 20
            while i not in room.members and time.monotonic() < deadline:
                time.sleep(.1)
            assert i in room.members
        print('NATIVE_RECOVERY_STARTED', args.fault, args.players, flush=True)
        for process in processes:
            process.wait(timeout=max(1, 360 - (time.monotonic() - started)))
        all_rows = [[json.loads(line) for path in sorted(output.glob(f'epoch-*/{i}.jsonl'))
                     for line in path.read_text().splitlines()] for i in range(args.players)]
        rows = [[r for r in records if r['epoch'] == room.epoch] for records in all_rows]
        saves = [json.loads((output / f'saves-{i}/current.json').read_bytes()) for i in range(args.players)]
        identical = all(len(row) == len(rows[0]) and all(a['state'] == b['state'] and a['inputs'] == b['inputs']
                         for a, b in zip(rows[0], row)) for row in rows[1:])
        stages = sorted({r['state']['stage'] + 1 for r in rows[0] if r['state']['scene'] == 0})
        first = [next(r['state'] for r in all_rows[0] if r['epoch'] == epoch and r['state']['scene'] == 0)
                 for epoch in (0, room.epoch)]
        first = [{k: v for k, v in state.items() if k != 'stage_checkpoint'} for state in first]
        summary = {'exit_codes': [p.returncode for p in processes], 'error': room.error, 'recovery': list(room.recovery_history),
                   'identical': identical, 'stage_start_restored': first[0] == first[1], 'stages': stages,
                   'saves_identical': all(value == saves[0] for value in saves), 'save_files': sorted(saves[0]['files']),
                   'elapsed': round(time.monotonic() - started, 2)}
        results = [json.loads((output / f'{i}.result.json').read_bytes()) for i in range(args.players)]
        summary.update(process_ids=[r['process_ids'] for r in results], window_handles=[r['window_handles'] for r in results],
                       scene_reloads=[r['scene_reloads'] for r in results], transport_resumes=[r['transport_resumes'] for r in results])
        (output / 'summary.json').write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary), flush=True)
        assert summary['exit_codes'] == [0] * args.players and room.error is None, summary
        if args.fault == 'disconnect':
            assert room.epoch == 0 and all(len(r['process_ids']) == 1 for r in results), summary
            assert sum(r['transport_resumes'] for r in results) >= 3, summary
        else:
            assert room.epoch == (2 if args.fault in ('repeat', 'reloadfail') else 1) and summary['stage_start_restored'], summary
            if args.fault in ('desync', 'repeat'):
                assert all(len(r['process_ids']) == len(r['window_handles']) == 1 for r in results), summary
                assert all(r['scene_reloads'] == room.epoch for r in results), summary
            elif args.fault == 'reloadfail':
                assert all(len(r['process_ids']) == 2 for r in results), summary
        assert identical and stages == ([1, 2] if args.frames >= 2500 else [1]) and summary['saves_identical'], summary
        original = json.loads(args.restore.read_bytes())
        assert set(original['files']) <= set(saves[0]['files']), 'Lost character saves'
        if args.players == 2:
            assert original['files']['chr_data03.sv4'] == saves[0]['files']['chr_data03.sv4'], 'Inactive 3P save changed'
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
    parser.add_argument('--index', type=int)
    parser.add_argument('--output')
    parser.add_argument('--port', type=int)
    parser.add_argument('--token')
    parser.add_argument('--game', type=Path, default=ROOT / 'runtime/app')
    parser.add_argument('--restore', type=Path, default=ROOT / 'analysis/campaign-save-1/saves-0/current.json')
    parser.add_argument('--stage', type=Path, default=ROOT / 'analysis/native-stage-restore-3/0.stage-0.json')
    parser.add_argument('--tag', default='native-recovery')
    parser.add_argument('--players', type=int, choices=(2, 3), default=3)
    parser.add_argument('--frames', type=int, default=3000)
    parser.add_argument('--fault', choices=('disconnect', 'desync', 'repeat', 'reloadfail', 'crash', 'exception'), default='disconnect')
    options = parser.parse_args()
    child(options) if options.index is not None else parent(options)
