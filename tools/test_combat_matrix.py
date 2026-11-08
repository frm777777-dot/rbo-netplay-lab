"""Silent native regression matrix; synthetic characters never reach real saves.

Exercises all 14 job/sex forms through ordinary input, with learned skill level
1 and repeated motion inputs. Observed action IDs and command opcodes measure
coverage; sending a motion alone is not evidence that a named skill activated.
Requires a local stage-entry fixture from test_netplay_campaign.py.
"""
import argparse
import base64
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
import test_support
from netplay_launcher import isolated_game
from netplay_campaign import run_campaign
from netplay_protocol import fingerprint
from netplay_transport import RecoveryRoom, RecoveryServer, RecoveryClient
from netplay_saves import SaveStore, encode, materialize


def stage_fixture(path, jobs):
    stage = json.loads(path.read_bytes())
    template = bytearray(base64.b64decode(stage['characters'])[:0x660])
    characters = bytearray(0x660 * 3)
    for slot in range(3):
        record = bytearray(template)
        struct.pack_into('<i', record, 0, jobs[slot] if slot < len(jobs) else -1)
        record[4:24] = (f'TEST{slot}'.encode() + bytes(20))[:20]
        struct.pack_into('<8I', record, 0x18, 20, 20, *([30] * 6))
        struct.pack_into('<300I', record, 0x54, *([1] * 300))
        characters[slot * 0x660:(slot + 1) * 0x660] = record
    stage.update(characters=base64.b64encode(characters).decode(), stage=0, slots=[0, 1, 2])
    return stage


def motion(frame):
    # Native bits: right, left, up, down, A, B, jump, guard.
    if frame < 600:
        return 16 if frame % 60 < 12 else 1
    frame -= 600
    if frame < 180:
        return 1
    sequence = ((16,), (32,), (64, 16), (64, 32), (128,),
                (8, 9, 1, 17), (8, 9, 1, 33), (8, 10, 2, 18),
                (8, 10, 2, 34), (8, 0, 8, 16), (8, 0, 8, 32),
                (1, 8, 9, 16), (1, 8, 9, 32), (2, 10, 8, 9, 1, 16),
                (2, 10, 8, 9, 1, 32), (16, 32, 16, 32))
    offset = frame - 180
    pattern = sequence[offset // 100 % len(sequence)]
    tick = offset % 100
    return pattern[min(tick // 4, len(pattern) - 1)] if tick < max(16, len(pattern) * 4) else 0


def child(args):
    out = args.output
    directory = isolated_game(args.game)
    config = directory / 'DATA/SaveData/SystemConfig.v4'
    values = list(struct.unpack('<20I', config.read_bytes()))
    values[2] = 0 if args.slot == 0 else 3
    values[6:12] = [16 if args.slot == 0 else 32] * 6
    values[15] = 3
    values[16] = 30 if args.slot == 0 else 60
    config.write_bytes(struct.pack('<20I', *values))
    store = SaveStore(out / f'saves-{args.slot}')
    client = RecoveryClient(('127.0.0.1', args.port), args.token, fingerprint(directory),
                            'host' if args.slot == 0 else 'guest', timeout=45)
    checkpoint = json.loads(client.ready(encode(store.load()) if args.slot == 0 else None))
    expected_job = struct.unpack_from('<i', base64.b64decode(client.stage['characters']), client.slot * 0x660)[0]
    store.commit(checkpoint)
    materialize(checkpoint, directory / 'DATA/SaveData')
    actions, opcodes, bindings = set(), set(), set()
    battle = attack_samples = 0

    def control(frame, slot, meta, d):
        nonlocal battle, attack_samples
        if meta['scene'] != 0:
            return 0
        battle += 1
        node = d.uint(0x22e84ec)
        while node:
            blob = d.read(node, 0x974)
            character = struct.unpack_from('<I', blob, 0x818)[0]
            bindings.add((struct.unpack_from('<I', blob, 0x34)[0], character))
            # +0x34 is character kind, passed to the command VM by 0x463d25.
            # The fixture uses distinct jobs, so each group-0 player root can
            # be matched to its save record without guessing a pointer layout.
            if struct.unpack_from('<I', blob, 0x34)[0] == expected_job:
                actions.add(blob[0x60])
                pending = [blob]
                while pending:
                    part = pending.pop()
                    if struct.unpack_from('<I', part, 0x72c)[0] and struct.unpack_from('<I', part, 0x768)[0]:
                        attack_samples += 1
                    for head in range(0x6a0, 0x6d0, 8):
                        command = struct.unpack_from('<I', part, head)[0]
                        seen = set()
                        while command and command not in seen:
                            seen.add(command)
                            command, pointer = d.words(command, 2)
                            opcodes.add(d.uint(pointer))
                    child_node = struct.unpack_from('<I', part, 0x14)[0]
                    while child_node:
                        descendant = d.read(child_node, 0x974)
                        pending.append(descendant)
                        child_node = struct.unpack_from('<I', descendant, 0xc)[0]
            node = struct.unpack_from('<I', blob, 4)[0]
        if battle == 400:
            print('OBSERVED_ACTIONS', sorted(actions), 'COMMANDS', sorted(opcodes), flush=True)
            assert actions, 'No native action observations'
        return motion(battle + slot * 12)

    try:
        result = run_campaign(directory, client, out / f'{args.slot}.jsonl', seconds=300,
                              control=control, full_log=True, checkpoint=checkpoint, save_store=store,
                              stage=client.stage)
        result.update(actions=sorted(actions), opcodes=sorted(opcodes), battle_frames=battle,
                      bindings=sorted(bindings), transport_resumes=client.transport_resumes, attack_samples=attack_samples)
        (out / f'{args.slot}.result.json').write_text(json.dumps(result))
    finally:
        client.close()


def case(args, jobs):
    out = ROOT / 'analysis' / args.tag / ('jobs-' + '-'.join(map(str, jobs)))
    out.mkdir(parents=True, exist_ok=False)
    room = RecoveryRoom(secrets.token_hex(16), secrets.token_hex(16), frames=args.frames,
                        players=len(jobs), timeout=45, diagnostics_dir=out / 'events')
    room.stage = stage_fixture(args.stage, jobs)
    server = RecoveryServer(room, ('127.0.0.1', 0))
    thread = threading.Thread(target=server.serve)
    thread.start()
    children, logs = [], []
    started = time.monotonic()
    try:
        for slot in range(len(jobs)):
            log = (out / f'{slot}.txt').open('w', encoding='utf-8')
            logs.append(log)
            command = [sys.executable, '-B', '-X', 'utf8', str(Path(__file__).resolve()), '--slot', str(slot),
                       '--output', str(out), '--port', str(server.address[1]), '--game', str(args.game),
                       '--token', room.host_token if slot == 0 else room.token]
            children.append(subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                             creationflags=subprocess.CREATE_NO_WINDOW))
            deadline = time.monotonic() + 30
            while slot not in room.members and time.monotonic() < deadline:
                time.sleep(.1)
            assert slot in room.members
        print('COMBAT_CASE', jobs, flush=True)
        for p in children:
            p.wait(timeout=max(1, 360 - (time.monotonic() - started)))
        assert [p.returncode for p in children] == [0] * len(jobs), str(out)
        rows = [[json.loads(s) for s in (out / f'{i}.jsonl').read_text().splitlines()] for i in range(len(jobs))]
        results = [json.loads((out / f'{i}.result.json').read_bytes()) for i in range(len(jobs))]
        assert all(len(r) == args.frames for r in rows)
        assert all(all(a['state'] == b['state'] and a['inputs'] == b['inputs'] for a, b in zip(rows[0], r)) for r in rows[1:])
        assert room.epoch == 0 and room.error is None
        assert all(len(r['actions']) >= 4 and r['attack_samples'] > 0 for r in results), results
        summary = {'jobs': jobs, 'frames': args.frames, 'identical': True, 'stage_restarts': room.epoch,
                   'actions': [r['actions'] for r in results], 'opcodes': [r['opcodes'] for r in results],
                   'attack_samples': [r['attack_samples'] for r in results],
                   'seconds': round(time.monotonic() - started, 2)}
        (out / 'summary.json').write_text(json.dumps(summary, indent=2))
        print('COMBAT_PASS', json.dumps(summary), flush=True)
        return summary
    finally:
        for p in children:
            if p.poll() is None:
                p.terminate()
                p.wait(10)
        for log in logs:
            log.close()
        server.close()
        thread.join(3)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--slot', type=int)
    p.add_argument('--output', type=Path)
    p.add_argument('--port', type=int)
    p.add_argument('--token')
    p.add_argument('--game', type=Path, default=ROOT / 'runtime/app')
    p.add_argument('--stage', type=Path, default=ROOT / 'analysis/native-stage-restore-3/0.stage-0.json')
    p.add_argument('--frames', type=int, default=2600)
    p.add_argument('--jobs', default='all')
    p.add_argument('--tag', default='combat-matrix')
    a = p.parse_args()
    if a.slot is not None:
        child(a)
    else:
        cases = ([list(range(i, i + 2)) for i in range(0, 14, 2)] if a.jobs == 'all'
                 else [list(map(int, group.split(','))) for group in a.jobs.split(';')])
        assert all(2 <= len(jobs) <= 3 and len(set(jobs)) == len(jobs) and all(0 <= j < 14 for j in jobs) for jobs in cases)
        summaries = [case(a, jobs) for jobs in cases]
        (ROOT / 'analysis' / a.tag / 'summary.json').write_text(json.dumps(summaries, indent=2))
