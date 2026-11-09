"""Localized one-player EX3, with an independent persistent save profile.

No server, sockets, shared input, online state checks or online save import.
The original game's timing and save points are preserved.
"""
import argparse
import contextlib
import json
import msvcrt
import os
from pathlib import Path
import struct
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from netplay_launcher import ROOT, isolated_game, pick_game
from netplay_campaign import DEFAULT_KEYS, patch
from netplay_korean import attach
from netplay_protocol import fingerprint, require, ProtocolError, GameCrashed
from netplay_saves import SaveStore, capture, materialize
from win32_debug import Debugger


@contextlib.contextmanager
def single_store(root):
    """Always use a dedicated child folder, even with a custom test data root."""
    store = SaveStore(Path(root) / 'saves/single')
    with (store.directory / 'session.lock').open('a+b') as lock:
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise ProtocolError('같은 저장 폴더를 사용하는 싱글 게임이 이미 실행 중입니다.') from None
        yield store


def run_single(directory, store, *, frames=0, seconds=None, control=None):
    """Test limits/control are only supplied by the development harness."""
    checkpoint = store.load()
    materialize(checkpoint, directory / 'DATA/SaveData')
    frame = 0
    mapping = None
    save_dirty = False
    result = {'mode': 'single', 'players': 1}

    def persist():
        nonlocal checkpoint
        current = capture(directory / 'DATA/SaveData', checkpoint['profile'], checkpoint['revision'] + 1)
        if current['files'] != checkpoint['files']:
            store.commit(current)
            checkpoint = current
            print('SINGLE_SAVE', checkpoint['revision'], sorted(checkpoint['files']), flush=True)

    def poll(d, ctx):
        nonlocal frame, mapping, save_dirty
        if frames and frame >= frames:
            return False
        config = d.uint(0xbc80c0)
        if mapping is None:
            kinds, keys = d.words(config, 9), d.words(config + 0x70, 9)
            for index in range(9):
                if kinds[index] != 1 or not 0 < keys[index] < 256:
                    d.write(config + index * 4, struct.pack('<I', 1))
                    d.write(config + 0x70 + index * 4, struct.pack('<I', DEFAULT_KEYS[index]))
            mapping = {'kind': d.words(config, 9), 'scan_codes': d.words(config + 0x70, 9)}
            print('LOCAL_1P_MAPPING', json.dumps(mapping), flush=True)
        pointer = d.uint(0xbc80c4)
        local = d.uint(pointer) & 0x1ff
        system = d.uint(0xbc80cc) & 0x1800
        if control is not None:
            scene = d.uint(0xbc91a0)
            meta = {'scene': scene, 'stage': d.uint(0xb07948),
                    'jobs': [struct.unpack('<i', d.read(0x25109c8 + i * 0x660, 4))[0] for i in range(3)],
                    'selection': [[d.uint(0x4a2920 + i * 0x1018 + off) for off in (0x44, 0x4c, 0xc)]
                                  for i in range(3)] if scene == 4 else None,
                    'pause': d.words(0xbc7af8, 4) if scene == 0 else None}
            value = control(frame, meta, d)
            local, system = value & 0x1ff, (value >> 9) & 0x1800
        # Force a single participant; local 2P/3P key mappings cannot join.
        inputs = (local, 0, 0)
        d.write(pointer, struct.pack('<3I', *inputs))
        processors = d.uint(0xbc80bc)
        for index, value in enumerate(inputs):
            d.write(d.uint(processors + index * 4) + 4, struct.pack('<I', value))
        d.write(0xbc80cc, struct.pack('<I', system))
        d.write(0xade9d0, bytes(4))
        if save_dirty or frame % 120 == 0:
            persist()
            save_dirty = False
        frame += 1

    def created(d):
        require(d.base == 0x400000, 'Unsupported image base')
        attach(d, directory, title='RBO EX3 - Korean Single')
        patch(d, 0x427900, bytes.fromhex('8b4424048b4c'), bytes.fromhex('b801000000c3'))
        d.write(0x497578, struct.pack('<I', 2))  # Start at the normal title menu.
        patch(d, 0x477380, bytes.fromhex('e8'), bytes.fromhex('b801000000c3'))
        # Disable the separate finite replay recorder, preserving native saves.
        patch(d, 0x438edf, bytes.fromhex('8b0db064b000'), bytes.fromhex('e98700000090'))
        def saved(d, ctx):
            nonlocal save_dirty
            require(ctx.eax == 1, 'Native character save failed')
            save_dirty = True
        def first_map(d, ctx):
            if d.uint(0x2553c20) == 0xffffffff:
                d.write(0x2553c20, bytes(4))
        d.breakpoint(0x456c00, saved)
        d.breakpoint(0x467e70, first_map)
        d.breakpoint(0x43d4cc, poll)
        print('SINGLE_READY', d.pid, flush=True)

    debugger = Debugger(directory / 'rbo_ex3.exe')
    debugger.run(created, seconds)
    if debugger.exit_code not in (None, 0):
        raise GameCrashed(f'게임이 비정상 종료됐습니다 (0x{debugger.exit_code:08x}). 마지막 저장본은 보관했습니다.')
    if frames:
        require(frame == frames, f'Single-player test stopped early: {frame}/{frames}')
    # Also collect system/progress files written during a normal game shutdown.
    persist()
    result.update(frames=frame, mapping=mapping, save_revision=checkpoint['revision'],
                  save_files=sorted(checkpoint['files']), completed=True)
    return result


def play_single(source, root=ROOT, *, frames=0, seconds=None, control=None):
    source, root = Path(source).resolve(), Path(root).resolve()
    logs = root / 'logs' / f'{time.strftime("%Y%m%d-%H%M%S")}-single-{os.getpid()}-{time.time_ns() % 1000000}'
    with single_store(root) as store:
        fingerprint(source)
        directory = isolated_game(source)
        logs.mkdir(parents=True, exist_ok=True)
        print(f'싱글 저장 폴더: {store.directory}')
        print('한글 싱글 게임을 시작합니다. 1P 키 설정을 사용합니다.')
        print('온라인 캐릭터와 별도로 저장합니다. 처음 실행하면 새 캐릭터로 시작합니다.')
        print('종료할 때는 게임 창을 먼저 닫아주세요. 플레이 시간 제한은 없습니다.')
        print('설정 변경은 Settings-Online.cmd에서 할 수 있습니다.')
        (logs / 'runtime.txt').write_text(str(directory), encoding='utf-8')
        try:
            with (logs / 'bridge.txt').open('w', encoding='utf-8') as diagnostic, contextlib.redirect_stdout(diagnostic):
                result = run_single(directory, store, frames=frames, seconds=seconds, control=control)
        except BaseException as error:
            (logs / 'result.json').write_text(json.dumps({'completed': False, 'error': str(error)}, ensure_ascii=False), encoding='utf-8')
            print(f'싱글 저장본은 보관되어 있습니다: {store.directory}')
            print(f'확인용 기록: {logs}')
            raise
        (logs / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print('싱글 게임을 종료했습니다. 다음 실행에도 싱글 저장을 이어서 사용합니다.')
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--game', type=Path)
    args = parser.parse_args()
    print('RBO 한글 싱글 v0.7.2 — 한글 이벤트 오류 수정·온라인과 별도 저장\n')
    play_single(args.game or pick_game())


if __name__ == '__main__':
    try:
        main()
    except (OSError, ProtocolError, ValueError, EOFError, KeyboardInterrupt) as error:
        print(f'\n싱글 게임을 종료했습니다: {error}')
        raise SystemExit(1)
