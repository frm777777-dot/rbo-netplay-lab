"""Host/join launcher for the two/three-player native EX3 campaign."""
import argparse
import contextlib
import hashlib
import json
import msvcrt
import os
from pathlib import Path
import secrets
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from netplay_campaign import run_campaign
from netplay_protocol import ProtocolError, RecoveryNeeded, TransportError, GameCrashed, fingerprint, require
from netplay_recovery import RecoveryClient as Client, RecoveryRoom as Room, RecoveryServer as Server
from netplay_saves import SaveStore, encode, validate, materialize

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
    # Keys and display/audio preferences are local to each PC. SystemData.v4
    # and character saves are separate and come from the shared online store.
    # Play never writes these temporary files back into the installed game.
    for name in ('KeyConfig.v4', 'KeyConfig.cdt', 'SystemConfig.v4'):
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
    from netplay_korean import install
    install(source, target)
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
    parser.add_argument('--saves-dir', type=Path, default=ROOT / 'saves', help='Online save root')
    args = parser.parse_args()
    require(0 <= args.frames <= 200000, '프레임 제한 값이 올바르지 않습니다.')
    print('RBO 2~3인 온라인 실험판 v0.5.1 — 자동 재접속 / 스테이지 복구 / 한글')
    print('각 PC에서 게임의 1P 키 설정을 사용합니다. 친구는 자동으로 2P·3P가 됩니다.')
    print('공통 메뉴와 출발은 1P, 캐릭터 선택·생성과 전투는 각자 조작합니다.')
    print('호스트가 전원 저장하고 친구 PC에도 사본을 보관합니다.\n')
    players = (args.players if args.players is not None else pick_players()) if args.mode == 'host' else None
    source = args.game or pick_game()
    print('게임 파일을 확인하고 있습니다…')
    game_hash = fingerprint(source)
    directory = isolated_game(source)
    require(fingerprint(directory) == game_hash, '복사된 게임 파일 검증 실패')
    logs = ROOT / 'logs' / f'{time.strftime("%Y%m%d-%H%M%S")}-{args.mode}-{os.getpid()}'
    logs.mkdir(parents=True, exist_ok=True)
    server = thread = client = None
    save_store = checkpoint = None
    save_lock = None
    result = None
    try:
        if args.mode == 'host':
            save_store = SaveStore(args.saves_dir / 'host')
            save_lock = (save_store.directory / 'session.lock').open('a+b')
            if save_lock.tell() == 0:
                save_lock.write(b'0'); save_lock.flush()
            save_lock.seek(0)
            try:
                msvcrt.locking(save_lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                save_lock.close()
                save_lock = None
                raise ProtocolError('같은 저장 폴더를 사용하는 호스트가 이미 실행 중입니다.')
            checkpoint = save_store.load()
            room = Room(secrets.token_urlsafe(18), secrets.token_urlsafe(24),
                        frames=args.frames, delay=args.delay, timeout=45, players=players)
            server = Server(room, (args.bind, args.port))
            thread = threading.Thread(target=server.serve, daemon=True)
            thread.start()
            client = Client(('127.0.0.1', args.port), room.host_token, game_hash, 'host', timeout=45)
            print(f'서버 포트: TCP {args.port}')
            print(f'친구에게 전달할 방 코드: {room.token}')
            print('친구는 Join-Online.cmd에서 이 PC의 접속 주소와 방 코드를 입력합니다.')
            print('외부 인터넷 접속에는 공유기의 포트 연결과 방화벽 허용이 필요합니다.')
        else:
            address = args.address or input('호스트 PC 주소: ').strip()
            token = input('방 코드: ').strip()
            print('호스트에 연결하고 있습니다…')
            client = Client((address, args.port), token, game_hash, timeout=45)
        print(f'내 캐릭터: {client.slot + 1}P / 내 키 설정: 로컬 1P')
        print(f'{client.players}명이 연결될 때까지 기다린 뒤 함께 타이틀 화면을 엽니다.')
        initial_header = encode(checkpoint) if checkpoint is not None else None
        try:
            initial = client.ready(initial_header)
        except (RecoveryNeeded, TransportError):
            initial = client.reconnect(initial_header)
        checkpoint = json.loads(initial)
        validate(checkpoint)
        if save_store is None:
            save_store = SaveStore(args.saves_dir / 'replicas' / checkpoint['profile'])
            save_store.commit(checkpoint)
        materialize(checkpoint, directory / 'DATA/SaveData')
        print(f'온라인 저장 폴더: {save_store.directory}')
        print('1P가 타이틀에서 Z로 시작하고, 캐릭터 화면에서 모두 Z로 참가하세요.')
        print('연결이 끊기거나 진행이 어긋나면 자동으로 재접속하고 스테이지 시작 지점으로 돌아갑니다.')
        print('복구 중에는 게임 창이 다시 열립니다. 실행기 창은 닫지 마세요.')
        print('저장된 캐릭터는 유지되며, 복구가 반복되면 저장본을 유지한 채 타이틀로 돌아갑니다.')
        print('게임 창에 포커스를 두고 조작하세요. 창을 닫으면 방 전체가 종료됩니다.')
        print('플레이 시간 제한 없이 진행합니다. 종료하려면 게임 창을 닫으세요.' if not client.frames
              else f'개발 검사: {client.frames}프레임 뒤 종료합니다.')
        while True:
            generation_logs = logs if client.epoch == 0 else logs / f'recovery-{client.epoch:03d}'
            generation_logs.mkdir(exist_ok=True)
            try:
                with (generation_logs / 'bridge.txt').open('w', encoding='utf-8') as diagnostic, contextlib.redirect_stdout(diagnostic):
                    result = run_campaign(directory, client, generation_logs / 'frames.jsonl', seconds=None,
                                          checkpoint=checkpoint, save_store=save_store, stage=client.stage)
                break
            except (RecoveryNeeded, TransportError, GameCrashed) as error:
                print(f'일시 중단: {error}')
                client.request_recovery()
                checkpoint = json.loads(client.reconnect(initial_header))
                save_store.commit(checkpoint)
                directory = isolated_game(source)
                materialize(checkpoint, directory / 'DATA/SaveData')
                if client.stage is None:
                    print('저장된 캐릭터를 유지하고 타이틀 화면을 다시 엽니다.')
                else:
                    print(f'{client.stage["stage"] + 1}스테이지 시작 지점으로 복구합니다. 내 캐릭터: {client.slot + 1}P')
        (logs / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print('시험 세션을 정상 종료했습니다.')
        print(f'온라인 저장과 사본은 다음 실행에도 유지됩니다: {save_store.directory}')
    except Exception as error:
        (logs / 'result.json').write_text(json.dumps({'completed': False, 'error': str(error)}, ensure_ascii=False, indent=2), encoding='utf-8')
        if save_store is not None:
            print(f'마지막으로 확인된 저장본은 보관되어 있습니다: {save_store.directory}')
        print(f'문제 확인용 기록: {logs}')
        raise
    finally:
        try:
            if client:
                client.close()
            if server:
                (logs / 'server.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in server.room.audit))
                (logs / 'recovery.json').write_text(json.dumps(list(server.room.recovery_history), ensure_ascii=False, indent=2), encoding='utf-8')
                if server.room.failure_details:
                    (logs / 'divergence.json').write_text(json.dumps(server.room.failure_details, ensure_ascii=False, indent=2), encoding='utf-8')
                if server.room.checkpoint is not None and save_store is not None:
                    save_store.commit(server.room.checkpoint)
        finally:
            if server:
                server.close()
                thread.join(3)
            if save_lock:
                save_lock.close()


if __name__ == '__main__':
    try:
        main()
    except (OSError, ProtocolError, ValueError, KeyboardInterrupt) as error:
        print(f'\n세션을 종료했습니다: {error}')
        raise SystemExit(1)
