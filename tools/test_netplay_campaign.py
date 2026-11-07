"""Real native campaign checks; all mutations stay in isolated test processes.

--advance sets the native stage-finished flag after a short battle. It tests the
real clear/save/next-stage path, not a natural boss victory or an entire game.
"""
import argparse
import json
from pathlib import Path
import secrets
import struct
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
from netplay_protocol import Room, Server, Client, fingerprint
from netplay_launcher import isolated_game
from netplay_campaign import run_campaign
from netplay_saves import SaveStore, encode, validate, materialize


def child(args):
    directory = isolated_game(args.game)
    output = Path(args.out)
    if args.mixed_settings:
        # Match original configuration-tool output, with different preferences
        # on every peer. Check inheritance before changing this owned copy.
        config = directory / 'DATA/SaveData/SystemConfig.v4'
        assert config.read_bytes() == (args.game / 'DATA/SaveData/SystemConfig.v4').read_bytes()
        values = list(struct.unpack('<20I', config.read_bytes()))
        values[1] = 0  # Windowed, as required by the online launcher.
        values[2] = (0, 2, 3)[args.slot]  # 640, 800, 1024 pixel width.
        values[6:12] = [16 if args.slot == 0 else 32] * 6
        values[15] = (0, 3, 2)[args.slot]  # All audio, movies only, no BGM.
        values[16] = 30 if args.slot == 1 else 60
        config.write_bytes(struct.pack('<20I', *values))
        (output / f'{args.slot}.settings.json').write_text(json.dumps(values))
    (output / f'{args.slot}.runtime.txt').write_text(str(directory))
    client = Client(('127.0.0.1', args.port), args.token, fingerprint(directory),
                    'host' if args.slot == 0 else 'guest')
    store = SaveStore(output / f'saves-{args.slot}')
    if args.restore:
        store.commit(json.loads(args.restore.read_bytes()))
    initial = json.loads(client.ready(encode(store.load()) if args.slot == 0 else None))
    validate(initial)
    store.commit(initial)
    materialize(initial, directory / 'DATA/SaveData')
    battle_frames = 0
    advanced = False

    def control(frame, slot, meta, debugger):
        nonlocal battle_frames, advanced
        if args.perturb_state and frame == 100 and slot == client.players - 1:
            debugger.write(0x25109c8 + 32, struct.pack('<I', 98765))
        if meta['scene'] == 0:
            battle_frames += 1
            if args.advance and not advanced and battle_frames == 800:
                debugger.write(0xadff7c, struct.pack('<I', 7))
                advanced = True
                print('TEST_STAGE_CLEAR', frame, flush=True)
            if args.pause and battle_frames in (120, 240):
                return 256 if slot == 0 else 0
            if args.pause and 121 <= battle_frames < 240:
                return 256 if slot else 0  # Guests must not own pause menus.
            if 300 <= battle_frames < 450:
                return 1  # Identical right input, independent characters.
            return 16 if frame % 60 < 15 else 0
        if meta['scene'] == 6:
            snapshot = output / f'{args.slot}.characters-{meta["stage"]}.bin'
            if not snapshot.exists():
                snapshot.write_bytes(debugger.read(0x25109c8, 0x660 * 3))
        if slot == client.players - 1 and frame < 500:
            return 0  # A late participant must not be left out of selection.
        if meta['scene'] == 4:
            phase = meta['selection'][slot][0]
            if phase == 3 and slot and meta['jobs'][slot] in meta['jobs'][:slot]:
                return 1 if frame % 60 < 15 else 0
            if phase == 4:
                return (16 if frame % 600 < 480 else 256) if frame % 60 < 15 else 0
        return 16 if frame % 60 < 15 else 0

    try:
        result = run_campaign(directory, client, output / f'{args.slot}.jsonl', seconds=300,
                              control=control, full_log=True, checkpoint=initial, save_store=store,
                              stage=json.loads(args.stage_file.read_bytes()) if args.stage_file else None,
                              stage_observer=lambda value: (output / f'{args.slot}.stage-{value["stage"]}.json').write_text(json.dumps(value)))
        (output / f'{args.slot}.result.json').write_text(json.dumps(result))
    finally:
        client.close()


def parent(args):
    output = ROOT / 'analysis' / args.tag
    output.mkdir(parents=True, exist_ok=False)
    room = Room(secrets.token_hex(12), secrets.token_hex(12), frames=args.frames,
                players=args.players, timeout=60)
    server = Server(room, ('127.0.0.1', 0))
    thread = threading.Thread(target=server.serve)
    thread.start()
    processes, logs = [], []
    started = time.monotonic()
    try:
        for index in range(args.players):
            log = (output / f'{index}.txt').open('w', encoding='utf-8')
            logs.append(log)
            command = [sys.executable, '-B', '-X', 'utf8', str(Path(__file__).resolve()),
                       '--slot', str(index), '--port', str(server.address[1]), '--out', str(output),
                       '--token', room.host_token if index == 0 else room.token,
                       '--game', str(args.game)]
            if args.restore:
                command += ['--restore', str(args.restore)]
            if args.stage_file:
                command += ['--stage-file', str(args.stage_file)]
            for option in ('advance', 'pause', 'perturb_state', 'mixed_settings'):
                if getattr(args, option):
                    command += ['--' + option.replace('_', '-')]
            processes.append(subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                              creationflags=subprocess.CREATE_NO_WINDOW))
        print('CAMPAIGN_TEST_STARTED', [p.pid for p in processes], flush=True)
        for process in processes:
            process.wait(timeout=max(1, 330 - (time.monotonic() - started)))
        records = [[json.loads(line) for line in (output / f'{i}.jsonl').read_text().splitlines()]
                   for i in range(args.players)]
        saves = [json.loads((output / f'saves-{i}/current.json').read_bytes()) for i in range(args.players)]
        common = min(map(len, records))
        identical = all(all(row[f]['state'] == records[0][f]['state'] and
                            row[f]['inputs'] == records[0][f]['inputs'] for f in range(common))
                        for row in records[1:])
        stages = sorted({r['state']['stage'] + 1 for r in records[0] if r['state']['scene'] == 0})
        pauses = sorted({r['state']['pause'][0] for r in records[0] if r['state']['scene'] == 0})
        summary = {'exit_codes': [p.returncode for p in processes], 'error': room.error,
                   'frames': room.audit_count, 'identical': identical, 'stages': stages,
                   'pause_states': pauses, 'saves_identical': all(s == saves[0] for s in saves),
                   'save_revision': saves[0]['revision'], 'save_files': sorted(saves[0]['files']),
                   'elapsed': round(time.monotonic() - started, 2), 'forced_clear': args.advance,
                   'negative_control': args.perturb_state}
        summary['mixed_local_settings'] = args.mixed_settings
        (output / 'summary.json').write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary), flush=True)
        if args.perturb_state:
            assert room.error and 'diverged at frame 101' in room.error, summary
        else:
            assert summary['exit_codes'] == [0] * args.players and room.error is None, summary
            assert identical and common == args.frames and summary['saves_identical'], summary
            if args.advance:
                assert 1 in stages and 2 in stages, summary
            if args.pause:
                assert 0 in pauses and 2 in pauses and records[0][-1]['state']['pause'][0] == 0, summary
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
    parser.add_argument('--port', type=int)
    parser.add_argument('--token')
    parser.add_argument('--out')
    parser.add_argument('--tag', default='campaign-check')
    parser.add_argument('--game', type=Path, default=ROOT / 'runtime/app')
    parser.add_argument('--frames', type=int, default=7200)
    parser.add_argument('--players', type=int, choices=(2, 3), default=3)
    parser.add_argument('--restore', type=Path)
    parser.add_argument('--stage-file', type=Path)
    parser.add_argument('--advance', action='store_true')
    parser.add_argument('--pause', action='store_true')
    parser.add_argument('--perturb-state', action='store_true')
    parser.add_argument('--mixed-settings', action='store_true')
    options = parser.parse_args()
    child(options) if options.slot is not None else parent(options)
