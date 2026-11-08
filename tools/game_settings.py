"""Remember the local EX3 installation shared by online, single and settings."""
import json
import os
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parent.parent


def game_directory(value):
    text = str(value).strip().strip('"')
    if not text:
        raise ValueError('게임 폴더 경로를 입력해주세요.')
    source = Path(text).resolve()
    if source.name.lower() == 'rbo_ex3.exe':
        source = source.parent
    if not (source / 'rbo_ex3.exe').is_file():
        raise ValueError('해당 폴더에 rbo_ex3.exe가 없습니다.')
    if not (source / 'DATA').is_dir():
        raise ValueError('해당 폴더에 DATA 폴더가 없습니다.')
    return source


def read_game_settings(path):
    try:
        with path.open('r', encoding='utf-8') as stream:
            text = stream.read(65537)
        if len(text) > 65536:
            raise ValueError('설정 파일이 너무 큽니다.')
        saved = json.loads(text)
        value = saved.get('game_directory') if isinstance(saved, dict) else None
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError('설정 형식이 다릅니다.')
        return game_directory(value)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        print('기억한 게임 폴더를 사용할 수 없습니다. 게임 폴더를 다시 확인합니다.')
        return None


def save_game_settings(path, source):
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({'game_directory': str(source)}, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return True
    except OSError:
        print('게임 폴더를 기억하지 못했습니다. 이번에는 입력한 경로로 실행합니다.')
        print('실행기 ZIP을 쓰기 가능한 폴더에 완전히 풀어 사용해주세요.')
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def pick_game(*, force=False, settings_path=None, default=None):
    path = Path(settings_path) if settings_path is not None else ROOT / 'game-settings.json'
    previous = read_game_settings(path)
    if previous is None:
        try:
            previous = game_directory(default if default is not None else ROOT / 'runtime/app')
        except (OSError, ValueError):
            pass
    if previous is not None and not force:
        print(f'게임 폴더: {previous}')
        print('다른 게임 폴더를 사용하려면 Select-Game.cmd를 실행하세요.')
        return previous
    if previous is not None:
        print(f'현재 게임 폴더: {previous}')
    while True:
        suffix = ' (Enter=현재 폴더 유지)' if previous is not None else ''
        value = input(f'일본어판 rbo_ex3.exe와 DATA가 들어 있는 폴더 경로{suffix}: ')
        try:
            source = game_directory(previous if not value.strip() and previous is not None else value)
            break
        except (OSError, ValueError) as error:
            print(f'게임 폴더를 확인해주세요: {error}')
    if save_game_settings(path, source):
        print('게임 폴더를 기억했습니다. 다음 실행부터 자동으로 사용합니다.')
    elif force:
        raise OSError('게임 폴더 변경을 저장하지 못했습니다. 실행기 폴더의 쓰기 권한을 확인해주세요.')
    return source


if __name__ == '__main__':
    try:
        selected = pick_game(force=True)
        print(f'선택한 게임 폴더: {selected}')
        print('이제 Host/Join/Single/Settings 실행기를 열어주세요.')
    except (OSError, ValueError, EOFError, KeyboardInterrupt) as error:
        print(f'\n게임 폴더 선택을 마쳤습니다: {error}')
        raise SystemExit(1)
