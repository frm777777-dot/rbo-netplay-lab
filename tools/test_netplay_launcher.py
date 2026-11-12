"""Smoke test the actual host/join CLI paths, including isolated game copies."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_support  # Enforce silence in every owned game child.
from prepare_test_runtime import prepare

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--players', type=int, choices=(2, 3), default=2)
    parser.add_argument('--tag', default='network-launchers-v2')
    parser.add_argument('--prompt', action='store_true', help='Test the interactive host count prompt with Enter')
    parser.add_argument('--unlimited', action='store_true', help='Test the default unlimited launcher; close only its owned game')
    args = parser.parse_args()
    if args.prompt and args.players != 2:
        parser.error('--prompt tests the two-player default')
    output = ROOT / 'analysis' / args.tag
    output.mkdir(exist_ok=False)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    processes = []
    previous_logs = set((ROOT / 'logs').glob('*'))
    started = time.monotonic()
    try:
        for index, name in enumerate('ABC'[:args.players]):
            runtime = prepare(name)
            command = [sys.executable, '-u', '-X', 'utf8', '-B', str(ROOT / 'tools/netplay_launcher.py'),
                       'host' if index == 0 else 'join', '--game', runtime['directory'],
                       '--port', str(port), '--bind', '127.0.0.1', '--saves-dir', str(output / f'saves-{index}')]
            command += ['--join-settings', str(output / f'join-settings-{index}.json')]
            if not args.unlimited:
                command += ['--frames', '120']
            if index:
                command += ['--address', '127.0.0.1']
            elif not args.prompt:
                command += ['--players', str(args.players)]
            process = subprocess.Popen(command, stdin=subprocess.PIPE if index == 0 and args.prompt else subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding='utf-8',
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            processes.append(process)
            if index == 0:
                if args.prompt:
                    process.stdin.write('\n')
                    process.stdin.flush()
                listening = False
                prefix = '서버 포트: TCP '
                for line in process.stdout:
                    if line.startswith(prefix):
                        listening = True
                        break
                if not listening:
                    raise RuntimeError('Host did not create room')
        print('LAUNCHER_TEST_STARTED', [p.pid for p in processes], flush=True)
        if args.unlimited:
            while time.monotonic() - started < 120:
                new_logs = [p for p in (ROOT / 'logs').glob(f'*-host-{processes[0].pid}') if p not in previous_logs]
                if len(new_logs) == 1:
                    frame_log = new_logs[0] / 'frames.jsonl'
                    if frame_log.exists() and len(frame_log.read_text().splitlines()) >= 120:
                        bridge = (new_logs[0] / 'bridge.txt').read_text()
                        ids = [int(line.split()[1]) for line in bridge.splitlines() if line.startswith('CAMPAIGN_READY ')]
                        assert len(ids) == 1, ids
                        # Process-lifetime test for the native child this test
                        # started, identified by its own launcher's fresh log.
                        from win32_debug import api, Close, Terminate, D, P, W, check
                        open_process = api('OpenProcess', [D, W.BOOL, D], P)
                        handle = open_process(1, False, ids[0])
                        check(handle)
                        try:
                            check(Terminate(handle, 0))
                        finally:
                            Close(handle)
                        break
                time.sleep(0.5)
            else:
                raise TimeoutError('Unlimited launcher did not enter gameplay')
        messages = []
        for process in processes:
            stdout, _ = process.communicate(timeout=max(1, 150 - (time.monotonic() - started)))
            messages.append(stdout)
        result = {'exit_codes': [p.returncode for p in processes], 'elapsed': round(time.monotonic() - started, 2),
                  'players': args.players, 'default_prompt_tested': args.prompt,
                  'default_unlimited_tested': args.unlimited, 'join_without_stdin': True}
        result['pass'] = result['exit_codes'] == [0] * args.players
        for index, message in enumerate(messages):
            (output / f'{index}.txt').write_text(message, encoding='utf-8')
        (output / 'result.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2), flush=True)
        assert result['pass'], result
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


if __name__ == '__main__':
    main()
