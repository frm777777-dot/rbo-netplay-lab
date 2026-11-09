"""Local RBO volume preference and PID-scoped Windows audio session control.

No game memory, simulation state, endpoint/master volume or other apps are
modified. All COM interfaces stay on the worker thread that acquired them.
"""
import contextlib
import ctypes as C
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid

SETTINGS = Path(__file__).resolve().parent.parent / 'audio-settings.json'


def volume_value(value):
    if type(value) is not int or not 0 <= value <= 100:
        raise ValueError('볼륨은 0~100 사이의 정수여야 합니다.')
    return value


def read_volume(path=SETTINGS):
    """None means leave the user's existing Windows mixer setting alone."""
    try:
        with Path(path).open('r', encoding='utf-8') as stream:
            data = stream.read(4097)
        if len(data) > 4096:
            raise ValueError('볼륨 설정 파일이 너무 큽니다.')
        data = json.loads(data)
        if type(data) is not dict or data.get('format') != 1:
            raise ValueError('볼륨 설정 형식이 다릅니다.')
        return volume_value(data.get('volume'))
    except FileNotFoundError:
        return None


def save_volume(value, path=SETTINGS):
    value = volume_value(value)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({'format': 1, 'volume': value}, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        # Windows can briefly deny replacement while the live worker (or a
        # sync client) is reading. Keep the old file intact and retry briefly.
        for attempt in range(10):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(.05)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def configure_volume(path=SETTINGS):
    print('RBO 전체 볼륨 — 배경 음악과 효과음을 함께 조절합니다.')
    try:
        current = read_volume(path)
        print(f'현재 설정: {current}%' if current is not None else '현재 설정: Windows 볼륨 믹서 값 사용')
    except (OSError, ValueError) as error:
        print(f'기존 볼륨 설정을 읽지 못했습니다: {error}')
    print('0=무음, 100=최대. 예: 20을 입력하면 20%로 저장합니다.')
    while True:
        value = input('게임 볼륨 (0~100, Enter=변경 안 함): ').strip()
        if not value:
            return
        try:
            number = int(value)
            volume_value(number)
        except ValueError:
            print('0~100 사이의 정수를 입력해주세요.')
            continue
        save_volume(number, path)
        print(f'게임 볼륨을 {number}%로 저장했습니다.')
        print('이 버전으로 실행한 게임은 잠시 후 반영되며, 다음 실행에도 유지됩니다.')
        print('원래 소리 끄기 설정이나 Windows 음소거가 켜져 있으면 소리는 나지 않습니다.')
        return


class GUID(C.Structure):
    _fields_ = [('data1', C.c_uint32), ('data2', C.c_uint16), ('data3', C.c_uint16),
                ('data4', C.c_ubyte * 8)]

    @classmethod
    def parse(cls, value):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


P = C.c_void_p
HRESULT = C.c_long
ENUMERATOR_CLASS = GUID.parse('bcde0395-e52f-467c-8e3d-c4579291692e')
ENUMERATOR = GUID.parse('a95664d2-9614-4f35-a746-de8db63617e6')
MANAGER = GUID.parse('77aa99a0-1bd6-484f-8bc7-2c654c9a9b6f')
CONTROL2 = GUID.parse('bfb7ff88-7239-4fc9-8fa2-07c950be9c6d')
SIMPLE_VOLUME = GUID.parse('87ce5498-68d6-44e5-9215-6da47ef883d8')


def invoke(pointer, index, types=(), args=()):
    address = C.cast(pointer, C.POINTER(C.POINTER(P))).contents[index]
    return C.WINFUNCTYPE(HRESULT, P, *types)(address)(pointer, *args)


def checked(result):
    if result < 0:
        raise OSError(f'Windows audio error 0x{result & 0xffffffff:08x}')


@contextlib.contextmanager
def pointer():
    value = P()
    try:
        yield value
    finally:
        if value:
            invoke(value, 2)  # IUnknown::Release


@contextlib.contextmanager
def apartment():
    ole = C.WinDLL('ole32')
    ole.CoInitializeEx.argtypes, ole.CoInitializeEx.restype = [P, C.c_uint32], HRESULT
    ole.CoCreateInstance.argtypes = [C.POINTER(GUID), P, C.c_uint32, C.POINTER(GUID), C.POINTER(P)]
    ole.CoCreateInstance.restype = HRESULT
    ole.CoTaskMemFree.argtypes, ole.CoTaskMemFree.restype = [P], None
    ole.CoUninitialize.argtypes, ole.CoUninitialize.restype = [], None
    checked(ole.CoInitializeEx(None, 0))  # COINIT_MULTITHREADED, own worker thread.
    try:
        yield ole
    finally:
        ole.CoUninitialize()


def process_sessions(ole, pid):
    """Yield only single-process, non-expired output sessions for this game."""
    with pointer() as enumerator, pointer() as devices:
        checked(ole.CoCreateInstance(C.byref(ENUMERATOR_CLASS), None, 23, C.byref(ENUMERATOR), C.byref(enumerator)))
        checked(invoke(enumerator, 3, (C.c_int, C.c_uint32, C.POINTER(P)), (0, 1, C.byref(devices))))
        count = C.c_uint32()
        checked(invoke(devices, 3, (C.POINTER(C.c_uint32),), (C.byref(count),)))
        for index in range(count.value):
            try:
                with pointer() as device, pointer() as manager, pointer() as sessions:
                    checked(invoke(devices, 4, (C.c_uint32, C.POINTER(P)), (index, C.byref(device))))
                    checked(invoke(device, 3, (C.POINTER(GUID), C.c_uint32, P, C.POINTER(P)),
                                   (C.byref(MANAGER), 23, None, C.byref(manager))))
                    checked(invoke(manager, 5, (C.POINTER(P),), (C.byref(sessions),)))
                    length = C.c_int()
                    checked(invoke(sessions, 3, (C.POINTER(C.c_int),), (C.byref(length),)))
                    for item in range(length.value):
                        with pointer() as control, pointer() as control2, pointer() as volume:
                            checked(invoke(sessions, 4, (C.c_int, C.POINTER(P)), (item, C.byref(control))))
                            checked(invoke(control, 0, (C.POINTER(GUID), C.POINTER(P)), (C.byref(CONTROL2), C.byref(control2))))
                            owner, state = C.c_uint32(), C.c_int()
                            result = invoke(control2, 14, (C.POINTER(C.c_uint32),), (C.byref(owner),))
                            # S_OK only: a session shared with another process is not ours to change.
                            if result != 0 or owner.value != pid:
                                continue
                            checked(invoke(control, 3, (C.POINTER(C.c_int),), (C.byref(state),)))
                            if state.value == 2:  # AudioSessionStateExpired
                                continue
                            identity = P()
                            try:
                                checked(invoke(control2, 13, (C.POINTER(P),), (C.byref(identity),)))
                                key = C.wstring_at(identity)
                            finally:
                                if identity:
                                    ole.CoTaskMemFree(identity)
                            checked(invoke(control, 0, (C.POINTER(GUID), C.POINTER(P)), (C.byref(SIMPLE_VOLUME), C.byref(volume))))
                            yield key, volume
            except OSError:
                # An unplugged endpoint must not prevent trying other outputs.
                continue


def apply_volume(ole, pid, value, previous):
    volume_value(value)
    seen = {}
    for key, volume in process_sessions(ole, pid):
        if previous.get(key) != value:
            checked(invoke(volume, 3, (C.c_float, C.POINTER(GUID)), (value / 100.0, None)))
        seen[key] = value
    return seen


class VolumeController:
    """Best effort, outside the debugger/frame loop; never aborts a game."""
    def __init__(self, pid, path=SETTINGS):
        self.pid, self.path = pid, Path(path)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name='RBO volume', daemon=True)

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)

    def run(self):
        try:
            with apartment() as ole:
                previous, reported, last_write = {}, False, None
                while not self.stop.is_set():
                    try:
                        value = read_volume(self.path)
                        if value is not None:
                            stamp = self.path.stat().st_mtime_ns
                            # Saving the same percentage is an explicit request
                            # to reapply it after a manual Windows mixer change.
                            previous = apply_volume(ole, self.pid, value, previous if stamp == last_write else {})
                            last_write = stamp
                    except (OSError, ValueError) as error:
                        if not reported:
                            print(f'볼륨 설정을 적용하지 못했습니다. 게임은 계속 진행합니다: {error}', flush=True)
                            reported = True
                    self.stop.wait(.5)
        except Exception as error:
            print(f'볼륨 조절을 사용할 수 없습니다. 게임은 계속 진행합니다: {error}', flush=True)


def start_volume(pid):
    # Native tests are muted before game creation; do not touch real mixer values.
    if os.environ.get('RBO_TEST_SILENT') == '1':
        return None
    try:
        return VolumeController(pid).start()
    except Exception as error:
        print(f'볼륨 조절을 시작하지 못했습니다. 게임은 계속 진행합니다: {error}', flush=True)
        return None


if __name__ == '__main__':
    try:
        configure_volume()
    except (OSError, ValueError, EOFError, KeyboardInterrupt) as error:
        print(f'볼륨 설정을 마쳤습니다: {error}')
        raise SystemExit(1)
