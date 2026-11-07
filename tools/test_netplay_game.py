"""Run two or three real EX3 processes through TCP and compare every frame."""
import argparse
import json
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from netplay_protocol import Room, Server
from prepare_test_runtime import prepare

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tag', default='network1')
    parser.add_argument('--frames', type=int, default=2400)
    parser.add_argument('--delay', type=int, default=3)
    parser.add_argument('--perturb-state', action='store_true')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--keyboard-test', action='store_true')
    parser.add_argument('--players', type=int, choices=(2, 3), default=3)
    parser.add_argument('--dump-from', type=int)
    parser.add_argument('--unlimited', action='store_true', help='Use an unlimited room; host closes at --frames')
    parser.add_argument('--bounded-log', action='store_true', help='Exercise production log rotation')
    args = parser.parse_args()
    output = ROOT / 'analysis' / args.tag
    output.mkdir(parents=True, exist_ok=False)
    room = Room(secrets.token_urlsafe(24), secrets.token_urlsafe(24), frames=0 if args.unlimited else args.frames, delay=args.delay, timeout=60, players=args.players)
    server = Server(room, ('127.0.0.1', 0))
    thread = threading.Thread(target=server.serve)
    thread.start()
    processes, logs = [], []
    started = time.monotonic()
    try:
        names = ('A', 'B', 'C')[:args.players]
        for index, name in enumerate(names):
            runtime = prepare(name)
            token_file = output / f'{name}.token'
            token_file.write_text(room.host_token if index == 0 else room.token)
            command = [sys.executable, '-B', str(ROOT / 'tools/netplay_game.py'),
                       '--game', runtime['directory'], '--port', str(server.address[1]),
                       '--token-file', str(token_file), '--seconds', '480',
                       '--log', str(output / f'{name}.jsonl')]
            if args.keyboard_test:
                command.append('--keyboard-test')
            elif not args.live:
                command.append('--scripted')
            if args.dump_from is not None:
                command += ['--dump-from', str(args.dump_from)]
            if index == 0:
                command.append('--host')
                if args.unlimited:
                    command += ['--stop-after', str(args.frames)]
            if args.unlimited and not args.bounded_log:
                command.append('--full-log')
            if args.perturb_state and index == args.players - 1:
                command.append('--perturb-state')
            log = (output / f'{name}.txt').open('w', encoding='utf-8')
            logs.append(log)
            processes.append(subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                              creationflags=subprocess.CREATE_NO_WINDOW))
        print('NETWORK_TEST_STARTED', server.address, 'pids', [p.pid for p in processes], flush=True)
        while any(p.poll() is None for p in processes):
            if time.monotonic() - started > 520:
                raise TimeoutError('Integration test deadline exceeded')
            time.sleep(1)
        codes = [p.returncode for p in processes]
        result = {'exit_codes': codes, 'error': room.error, 'completed_slots': sorted(room.completed),
                  'server_frames': room.audit_count, 'elapsed': round(time.monotonic() - started, 2),
                  'unlimited': args.unlimited, 'stop_reason': room.stop_reason,
                  'delay': args.delay, 'negative_control': args.perturb_state,
                  'live_keyboard': args.live, 'keyboard_buffer_test': args.keyboard_test, 'players': args.players}
        (output / 'server.jsonl').write_text(''.join(json.dumps(r, separators=(',', ':')) + '\n' for r in room.audit))
        rows = []
        log_sizes = []
        for name in names:
            paths = [output / f'{name}.jsonl{suffix}' for suffix in ('.2', '.1', '')]
            paths = [path for path in paths if path.exists()]
            rows.append([json.loads(line) for path in paths for line in path.read_text().splitlines()])
            log_sizes.append(sum(path.stat().st_size for path in paths))
            if args.bounded_log:
                assert all(path.stat().st_size <= 8 * 1024 * 1024 for path in paths)
                assert len(paths) <= 3
        result['retained_log_bytes'] = log_sizes
        result['counts'] = [len(r) for r in rows]
        result['assigned_slots'] = [r[0]['slot'] if r else None for r in rows]
        if not args.perturb_state and codes == [0] * args.players:
            if args.unlimited:
                assert result['counts'][0] == args.frames
                assert min(result['counts']) >= args.frames - args.delay
                assert max(result['counts']) <= args.frames + args.delay + 1
                assert room.stop_reason == 'Player 1 ended the session'
            else:
                assert result['counts'] == [args.frames] * args.players
            assert set(result['assigned_slots']) == set(range(args.players))
            differences = {key: [] for key in ('inputs', 'state')}
            for frame, copies in enumerate(zip(*rows)):
                for key in differences:
                    if not all(copy[key] == copies[0][key] for copy in copies):
                        differences[key].append(frame)
                expected = [0, 0, 0]
                if frame >= args.delay:
                    for records in rows:
                        expected[records[0]['slot']] = records[frame - args.delay]['local_input']
                assert copies[0]['inputs'] == expected, (frame, expected, copies[0]['inputs'])
                if copies[0]['state']:
                    jobs = copies[0]['state']['jobs']
                    assert all(job >= 0 for job in jobs[:args.players])
                    assert all(job == -1 for job in jobs[args.players:]), jobs
            result['mismatches'] = {key: len(values) for key, values in differences.items()}
            result['entity_records_per_client'] = sum(r['state']['entities'] for r in rows[0] if r['state'])
            result['input_routing_verified'] = True
            result['active_character_count_verified'] = args.players
            result['controlled_key_isolation_sequence_verified'] = not args.live and args.frames >= 1920 + args.delay
            result['nonzero_local_input_frames'] = [sum(r['local_input'] != 0 for r in records) for records in rows]
            if args.keyboard_test:
                expected_nonzero = 240
                assert result['nonzero_local_input_frames'] == [expected_nonzero] * args.players, result
                for records in rows:
                    for row in records:
                        phase = (row['frame'] - 1800) // 120
                        pressed = 1800 <= row['frame'] < 2280 and phase in (row['slot'], 3)
                        assert row['native_local_input'] == row['local_input'] == (16 if pressed else 0), row
            result['pass'] = not any(differences.values()) and not room.error
        elif args.perturb_state:
            result['pass'] = all(code != 0 for code in codes) and 'diverged at frame 1801' in str(room.error)
        else:
            result['pass'] = False
        (output / 'result.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2), flush=True)
        if not result['pass']:
            raise SystemExit(1)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        for log in logs:
            log.close()
        server.close()
        thread.join(3)
        for token_file in output.glob('*.token'):
            token_file.unlink()


if __name__ == '__main__':
    main()
