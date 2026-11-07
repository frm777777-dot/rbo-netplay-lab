"""Host/join launcher for the limited two/three-player EX3 experiment."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from netplay_game import run_game
from netplay_protocol import Client, ProtocolError, Room, Server, fingerprint, require

ROOT = Path(__file__).resolve().parent.parent


def isolated_game(source):
    """Copy mutable files and hardlink immutable assets in an owned ASCII folder."""
    source = Path(source).resolve()
    identity = hashlib.sha256(str(source).encode('utf-8')).hexdigest()[:12]
    target = Path(tempfile.gettempdir()) / 'rbo-netplay-lab' / identity / secrets.token_hex(4)
    require(str(target).isascii(), '임시 폴더 경로에 한글이 있습니다. 영문 TEMP 경로가 필요합니다.')
    target.mkdir(parents=True, exist_ok=True)
    for folder in ('DATA', 'DATA/etc', 'DATA/SaveData', 'DATA/Movie'):
        (target / folder).mkdir(exist_ok=True)
    for name in ('rbo_ex3.exe', 'rbo_ex3.bmp'):
        if (source / name).is_file():
            shutil.copy2(source / name, target / name)
    for path in (source / 'DATA').glob('*.pac'):
        dest = target / 'DATA' / path.name
        if dest.exists():
            require(hashlib.sha256(dest.read_bytes()).digest() == hashlib.sha256(path.read_bytes()).digest(),
                    f'임시 게임 파일이 다릅니다: {dest}')
            continue
        try:
            os.link(path, dest)
        except OSError:
            shutil.copy2(path, dest)
    # Only keyboard configuration is inherited. Experimental play never writes
    # characters, progress or settings back into the user's installed game.
    for name in ('KeyConfig.v4', 'KeyConfig.cdt'):
        path = source / 'DATA/SaveData' / name
        dest = target / 'DATA/SaveData' / name
        if path.is_file():
            shutil.copy2(path, dest)
        elif dest.exists():
            dest.unlink()
    for name in ('ex3_old_config_copy.end', 'ex3_chr_convert.end', 'ex3_old_arena_flag.end'):
        (target / 'DATA/etc' / name).touch()
    record = str(target).encode('ascii') + b'\0'
    require(len(record) <= 260, '임시 폴더 경로가 너무 깁니다.')
    (target / 'DATA/etc/ex3_old_rbo_path.end').write_bytes(record.ljust(260, b'\0') * 3)
    for name in ('_FCommand1st.Txt', '_FCommand.Txt'):
        (target / name).write_text('BootScreenMode WINDOW\n', encoding='ascii')
    return target


def pick_game():
    default = ROOT / 'runtime/app'
    if (default / 'rbo_ex3.exe').is_file():
        return default
    text = input('일본어판 rbo_ex3.exe와 DATA가 들어 있는 폴더 경로: ').strip().strip('"')
    source = Path(text)
    if source.name.lower() == 'rbo_ex3.exe':
        source = source.parent
    require((source / 'rbo_ex3.exe').is_file(), '해당 폴더에 rbo_ex3.exe가 없습니다.')
    return source


def pick_players():
    while True:
        value = input('플레이 인원 (본인 포함 2 또는 3, Enter=2): ').strip()
        if value in ('', '2', '3'):
            return int(value or '2')
        print('2 또는 3을 입력해주세요.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('host', 'join'))
    parser.add_argument('--game', type=Path)
    parser.add_argument('--address')
    parser.add_argument('--port', type=int, default=47624)
    parser.add_argument('--frames', type=int, default=0, help='0 = no play-time limit (default); positive values are developer tests')
    parser.add_argument('--delay', type=int, default=3)
    parser.add_argument('--bind', default='0.0.0.0')
    parser.add_argument('--players', type=int, choices=(2, 3), help='Total players, including the host')
    args = parser.parse_args()
    require(0 <= args.frames <= 200000, '프레임 제한 값이 올바르지 않습니다.')
    print('RBO 2~3인 온라인 실험판 v0.3.0 — 플레이 시간 제한 없음')
    print('각 PC에서 게임의 1P 키 설정을 사용합니다. 친구는 자동으로 2P·3P가 됩니다.')
    print('캐릭터 선택·일반 스테이지 진행·세이브는 아직 지원하지 않습니다.\n')
    players = (args.players if args.players is not None else pick_players()) if args.mode == 'host' else None
    source = args.game or pick_game()
    print('게임 파일을 확인하고 있습니다…')
    game_hash = fingerprint(source)
    directory = isolated_game(source)
    require(fingerprint(directory) == game_hash, '복사된 게임 파일 검증 실패')
    logs = ROOT / 'logs' / f'{time.strftime("%Y%m%d-%H%M%S")}-{args.mode}-{os.getpid()}'
    logs.mkdir(parents=True, exist_ok=True)
    server = thread = client = None
    result = None
    try:
        if args.mode == 'host':
            room = Room(secrets.token_urlsafe(18), secrets.token_urlsafe(24),
                        frames=args.frames, delay=args.delay, timeout=180, players=players)
            server = Server(room, (args.bind, args.port))
            thread = threading.Thread(target=server.serve, daemon=True)
            thread.start()
            client = Client(('127.0.0.1', args.port), room.host_token, game_hash, 'host', timeout=180)
            print(f'서버 포트: TCP {args.port}')
            print(f'친구에게 전달할 방 코드: {room.token}')
            print('친구는 Join-Online.cmd에서 이 PC의 접속 주소와 방 코드를 입력합니다.')
            print('외부 인터넷 접속에는 공유기의 포트 연결과 방화벽 허용이 필요합니다.')
        else:
            address = args.address or input('호스트 PC 주소: ').strip()
            token = input('방 코드: ').strip()
            print('호스트에 연결하고 있습니다…')
            client = Client((address, args.port), token, game_hash, timeout=180)
        print(f'내 캐릭터: {client.slot + 1}P / 내 키 설정: 로컬 1P')
        print(f'로고·타이틀에서 약 30초 준비한 뒤, {client.players}명이 모두 준비되면 전투가 시작됩니다.')
        print('게임 창에 포커스를 두고 조작하세요. 창을 닫으면 방 전체가 종료됩니다.')
        print('플레이 시간 제한 없이 진행합니다. 종료하려면 게임 창을 닫으세요.' if not client.frames
              else f'개발 검사: {client.frames}프레임 뒤 종료합니다.')
        with (logs / 'bridge.txt').open('w', encoding='utf-8') as diagnostic, contextlib.redirect_stdout(diagnostic):
            result = run_game(directory, client, logs / 'frames.jsonl', seconds=None)
        (logs / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print('시험 세션을 정상 종료했습니다.')
    except Exception as error:
        (logs / 'result.json').write_text(json.dumps({'completed': False, 'error': str(error)}, ensure_ascii=False, indent=2), encoding='utf-8')
        raise
    finally:
        if client:
            client.close()
        if server:
            (logs / 'server.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in server.room.audit))
            server.close()
            thread.join(3)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ProtocolError, ValueError, KeyboardInterrupt) as error:
        print(f'\n세션을 종료했습니다: {error}')
        raise SystemExit(1)
