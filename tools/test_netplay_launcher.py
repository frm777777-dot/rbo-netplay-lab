"""Smoke test the actual host/join CLI paths, including isolated game copies."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prepare_test_runtime import prepare

ROOT = Path(__file__).resolve().parent.parent


def main():
    output = ROOT / 'analysis/network-launchers'
    output.mkdir(exist_ok=True)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    processes = []
    started = time.monotonic()
    try:
        for index, name in enumerate('ABC'):
            runtime = prepare(name)
            command = [sys.executable, '-u', '-X', 'utf8', '-B', str(ROOT / 'tools/netplay_launcher.py'),
                       'host' if index == 0 else 'join', '--game', runtime['directory'],
                       '--port', str(port), '--frames', '120', '--bind', '127.0.0.1']
            if index:
                command += ['--address', '127.0.0.1']
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding='utf-8',
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            processes.append(process)
            if index == 0:
                token = None
                prefix = '친구에게 전달할 방 코드: '
                for line in process.stdout:
                    if line.startswith(prefix):
                        token = line[len(prefix):].strip()
                        break
                if not token:
                    raise RuntimeError('Host did not create room')
            else:
                process.stdin.write(token + '\n')
                process.stdin.flush()
        print('LAUNCHER_TEST_STARTED', [p.pid for p in processes], flush=True)
        messages = []
        for process in processes:
            stdout, _ = process.communicate(timeout=max(1, 150 - (time.monotonic() - started)))
            messages.append(stdout.replace(token, '[room code omitted]'))
        result = {'exit_codes': [p.returncode for p in processes], 'elapsed': round(time.monotonic() - started, 2)}
        result['pass'] = result['exit_codes'] == [0, 0, 0]
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
