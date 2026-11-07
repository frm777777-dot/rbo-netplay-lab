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
from netplay_launcher import isolated_game
from netplay_campaign import run_campaign
from netplay_protocol import fingerprint, RecoveryNeeded, TransportError, GameCrashed
from netplay_recovery import RecoveryRoom, RecoveryServer, RecoveryClient
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
    try:
        while True:
            generation = output / f'epoch-{client.epoch}'
            generation.mkdir(exist_ok=True)
            directory = isolated_game(args.game)
            materialize(checkpoint, directory / 'DATA/SaveData')
            battle_frames = 0
            cleared = False

            def control(frame, slot, meta, debugger):
                nonlocal battle_frames, cleared
                if client.epoch == 0 and frame == 360 and slot == client.players - 1:
                    print('INJECT_FAULT', args.fault, flush=True)
                    if args.fault == 'disconnect':
                        client.socket.shutdown(socket.SHUT_RDWR)
                        client.socket.close()
                    elif args.fault == 'desync':
                        debugger.write(0x25109c8 + 32, struct.pack('<I', 98765))
                    elif args.fault == 'crash':
                        Terminate(debugger.handle, 0xdead)
                    else:
                        # An actual native CPU fault, not just TerminateProcess.
                        # This is the owned game's next input-processing routine.
                        debugger.write(0x43da00, b'\x0f\x0b')
                if meta['scene'] == 0:
                    battle_frames += 1
                    if client.epoch and not cleared and battle_frames == 650:
                        debugger.write(0xadff7c, struct.pack('<I', 7))
                        cleared = True
                return 16 if frame % 60 < 15 else 0

            try:
                result = run_campaign(directory, client, generation / f'{args.index}.jsonl', seconds=180,
                                      control=control, full_log=True, checkpoint=checkpoint,
                                      save_store=store, stage=client.stage)
                (output / f'{args.index}.result.json').write_text(json.dumps(result))
                break
            except (RecoveryNeeded, TransportError, GameCrashed) as error:
                print('RECOVERY', client.epoch, str(error), flush=True)
                client.request_recovery()
                checkpoint = json.loads(client.reconnect(encode(initial) if client.slot == 0 else None))
                store.commit(checkpoint)
                assert client.slot == args.index, 'Seat changed'
    finally:
        client.close()


def parent(args):
    output = ROOT / 'analysis' / args.tag
    output.mkdir(exist_ok=False)
    room = RecoveryRoom(secrets.token_hex(16), secrets.token_hex(16), frames=4000, players=args.players, timeout=45)
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
            process.wait(timeout=max(1, 240 - (time.monotonic() - started)))
        rows = [[json.loads(line) for line in (output / f'epoch-{room.epoch}/{i}.jsonl').read_text().splitlines()]
                for i in range(args.players)]
        saves = [json.loads((output / f'saves-{i}/current.json').read_bytes()) for i in range(args.players)]
        identical = all(len(row) == len(rows[0]) and all(a['state'] == b['state'] and a['inputs'] == b['inputs']
                         for a, b in zip(rows[0], row)) for row in rows[1:])
        stages = sorted({r['state']['stage'] + 1 for r in rows[0] if r['state']['scene'] == 0})
        first = [next(json.loads(line)['state'] for line in (output / f'epoch-{epoch}/0.jsonl').read_text().splitlines()
                      if json.loads(line)['state']['scene'] == 0) for epoch in (0, room.epoch)]
        summary = {'exit_codes': [p.returncode for p in processes], 'error': room.error, 'recovery': list(room.recovery_history),
                   'identical': identical, 'stage_start_restored': first[0] == first[1], 'stages': stages,
                   'saves_identical': all(value == saves[0] for value in saves), 'save_files': sorted(saves[0]['files']),
                   'elapsed': round(time.monotonic() - started, 2)}
        (output / 'summary.json').write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary), flush=True)
        assert summary['exit_codes'] == [0] * args.players and room.error is None and room.epoch == 1, summary
        assert identical and summary['stage_start_restored'] and stages == [1, 2] and summary['saves_identical'], summary
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
    parser.add_argument('--fault', choices=('disconnect', 'desync', 'crash', 'exception'), default='disconnect')
    options = parser.parse_args()
    child(options) if options.index is not None else parent(options)
