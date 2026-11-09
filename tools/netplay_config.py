"""Build a Korean EX3 configuration tool from the user's original executable.

Only translation/layout instructions ship with the launcher, never the game
configuration executable. SystemConfig.v4 stays local to each player's PC.
"""
import argparse
import ctypes
from ctypes import wintypes as W
import hashlib
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

ORIGINAL_SHA256 = 'b8dc7b0dcc56be0c4e427fb9d51a9eb7414476e230d460cde1ae1253172f791b'

# Fixed-size strings in the verified original PE. Preserve addresses and format
# arguments; UTF-8 is enabled for this process only by its embedded manifest.
STRINGS = [
    (0xc170, 64, '화면 크기: 640 × 480'),
    (0xc1b0, 64, '화면 크기: 가상 PSP'),
    (0xc1f0, 64, '화면 크기: 800 × 600'),
    (0xc230, 64, '화면 크기: 1024 × 768'),
    (0xc270, 64, '모든 소리 켜기'),
    (0xc2b0, 64, '배경 음악 끄기'),
    (0xc2f0, 64, '동영상 외의 소리 끄기'),
    (0xc330, 64, '화면 갱신: 보통'),
    (0xc370, 64, '화면 갱신: 낮음'),
    (0xc3b0, 28, '플레이어 %d 키 설정'),
    (0xc3cc, 8, '없음'),
    (0xc3e8, 24, 'RBO EX3 한글 설정'),
    (0xc400, 48, '키 설정을 저장하지 못했습니다.'),
    (0xc430, 36, '환경 설정 저장 실패'),
    (0xc454, 8, '오류'),
    (0xc45c, 68, '패드가 없습니다.\r\n키 설정을 초기화할까요?'),
    (0xc4a0, 20, '패드 오류'),
    (0xc4b4, 36, 'RBO EX3 한글 설정'),
]

MANIFEST = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <assemblyIdentity type="win32" name="RBO.Netplay.Korean.Config" version="1.0.0.0"/>
  <application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings>
    <activeCodePage xmlns="http://schemas.microsoft.com/SMI/2019/WindowsSettings">UTF-8</activeCodePage>
  </windowsSettings></application>
</assembly>'''


def field(value):
    if isinstance(value, int):
        return struct.pack('<HH', 0xffff, value)
    return value.encode('utf-16le') + b'\0\0'


def dialog(title, size, controls, exstyle=0):
    data = bytearray(struct.pack('<IIHhhhh', 0x80c800c0, exstyle, len(controls), 0, 0, *size))
    data += b'\0\0\0\0' + field(title) + struct.pack('<H', 9) + field('맑은 고딕')
    for ident, kind, text, rect, style in controls:
        data += b'\0' * (-len(data) % 4)
        data += struct.pack('<IIhhhhH', style, 0, *rect, ident)
        data += field(kind) + field(text) + b'\0\0'
    return bytes(data)


def translated_dialogs():
    button, static, combo = 128, 130, 133
    controls = [
        (65535, static, '온라인에서는 각자 1P 키 설정을 사용합니다.', (12, 10, 228, 14), 0x50000000),
        (65535, static, '설정을 저장한 뒤 Host / Join을 실행하세요.', (12, 26, 228, 14), 0x50000000),
        (1017, button, '전체 화면 (온라인은 창 모드로 실행)', (12, 49, 228, 14), 0x50010003),
        (1009, combo, '', (12, 69, 226, 100), 0x50210003),
        (1010, combo, '', (12, 94, 226, 80), 0x50210003),
        (1011, combo, '', (12, 119, 226, 65), 0x50210003),
        (1004, button, '1P 키 설정 — 온라인에서 사용할 키', (12, 148, 226, 22), 0x50010000),
        (1005, button, '2P 키 설정 (한 PC에서 플레이할 때)', (12, 176, 226, 22), 0x50010000),
        (1006, button, '3P 키 설정 (한 PC에서 플레이할 때)', (12, 204, 226, 22), 0x50010000),
        (65535, static, '안정성 보정', (12, 243, 100, 12), 0x50000000),
        (65535, static, '텍스처 화질', (138, 243, 100, 12), 0x50000000),
        (1002, 'msctls_trackbar32', '', (12, 260, 100, 20), 0x50010009),
        (1001, 'msctls_trackbar32', '', (138, 260, 100, 20), 0x50010009),
        (65535, static, '최소', (12, 282, 35, 12), 0x50000000),
        (65535, static, '최대', (84, 282, 35, 12), 0x50000000),
        (65535, static, '낮음', (138, 282, 35, 12), 0x50000000),
        (65535, static, '높음', (210, 282, 35, 12), 0x50000000),
        (1, button, '저장', (69, 310, 78, 22), 0x50010001),
        (2, button, '취소', (160, 310, 78, 22), 0x50010000),
    ]
    keys = [(1016, static, '', (12, 10, 196, 14), 0x50020001),
            (65535, static, '오른쪽 칸을 선택하고 원하는 키를 누르세요.', (12, 29, 196, 14), 0x50000000),
            (65535, static, '온라인에서는 키보드만 지원합니다.', (12, 45, 196, 14), 0x50000000)]
    for index, (ident, label) in enumerate(zip(
            (1013, 1014, 1015, 1017, 1018, 1019, 1020, 1021, 1022),
            ('위', '아래', '왼쪽', '오른쪽', '약공격 / 결정', '강공격 / 취소', '방어', '특수 동작', '일시정지'))):
        y = 67 + index * 21
        keys += [(65535, static, label, (12, y + 3, 70, 13), 0x50000000),
                 (ident, 129, '', (86, y, 122, 17), 0x50810980 if index == 0 else 0x50810880)]
    keys += [(1, button, '확인', (64, 266, 66, 22), 0x50010001),
             (2, button, '취소', (142, 266, 66, 22), 0x50010000)]
    # The original's hidden owner keeps its dialog off the taskbar. Give the
    # settings window its own taskbar entry so users can find it again.
    return {102: dialog('RBO EX3 한글 설정', (250, 344), controls, 0x40000),
            104: dialog('키 설정', (220, 300), keys)}


def update_resources(path):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    begin = kernel.BeginUpdateResourceW
    begin.argtypes, begin.restype = [W.LPCWSTR, W.BOOL], W.HANDLE
    update = kernel.UpdateResourceW
    update.argtypes = [W.HANDLE, ctypes.c_void_p, ctypes.c_void_p, W.WORD, ctypes.c_void_p, W.DWORD]
    update.restype = W.BOOL
    end = kernel.EndUpdateResourceW
    end.argtypes, end.restype = [W.HANDLE, W.BOOL], W.BOOL
    handle = begin(str(path), False)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        resources = [(5, ident, 1041, value) for ident, value in translated_dialogs().items()]
        resources.append((24, 1, 0, MANIFEST))
        for kind, ident, language, value in resources:
            buffer = ctypes.create_string_buffer(value)
            if not update(handle, kind, ident, language, buffer, len(value)):
                raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        end(handle, True)
        raise
    if not end(handle, False):
        raise ctypes.WinError(ctypes.get_last_error())


def build(source):
    source = Path(source).resolve()
    original = source / 'RBOEx3Config.exe'
    if not original.is_file():
        raise ValueError('게임 폴더에 RBOEx3Config.exe가 없습니다. EX3 원본 설정 프로그램이 필요합니다.')
    data = bytearray(original.read_bytes())
    if hashlib.sha256(data).hexdigest() != ORIGINAL_SHA256:
        raise ValueError('이 버전의 RBOEx3Config.exe는 아직 지원하지 않습니다. 원본 파일은 그대로 보존했습니다.')
    if sys.getwindowsversion().build < 18362:
        raise ValueError('한글 설정 프로그램은 Windows 10 1903 이상에서 사용할 수 있습니다.')
    # Use Unicode dialog creation for UTF-16 resource text. The ANSI dialog
    # loader truncates some Korean labels even with a UTF-8 process manifest.
    assert data.count(b'DialogBoxParamA\0') == 1
    data = data.replace(b'DialogBoxParamA\0', b'DialogBoxParamW\0')
    for offset, capacity, text in STRINGS:
        value = text.encode('utf-8') + b'\0'
        if len(value) > capacity:
            raise ValueError(f'설정 번역 문자열이 너무 깁니다: {text}')
        data[offset:offset + capacity] = value.ljust(capacity, b'\0')
    target = source / 'RBOEx3Config-KO.exe'
    handle, temporary = tempfile.mkstemp(prefix='rbo-config-ko-', suffix='.exe', dir=source)
    os.close(handle)
    temp = Path(temporary)
    try:
        temp.write_bytes(data)
        update_resources(temp)
        if not target.is_file() or target.read_bytes() != temp.read_bytes():
            os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--game', type=Path)
    parser.add_argument('--build-only', action='store_true')
    args = parser.parse_args()
    if not args.build_only:
        from game_audio import configure_volume
        configure_volume()
    if args.game:
        source = args.game.resolve()
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from netplay_launcher import pick_game
        source = pick_game().resolve()
    program = build(source)
    print('RBO EX3 한글 설정 프로그램을 준비했습니다.')
    print('호스트와 친구 모두 각자 1P 키 설정을 바꾸세요. 온라인에서는 키보드만 지원합니다.')
    print('화면·소리·화질은 각 PC의 설정을 사용합니다. 온라인은 창 모드로 실행합니다.')
    print('설정을 저장하고 닫은 뒤 Host-Online.cmd 또는 Join-Online.cmd를 실행하세요.')
    if not args.build_only:
        (source / 'DATA/SaveData').mkdir(parents=True, exist_ok=True)
        subprocess.run([str(program)], cwd=source, check=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        print(f'\n설정 프로그램을 열지 못했습니다: {error}')
        raise SystemExit(1)
