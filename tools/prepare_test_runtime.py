"""Prepare isolated ASCII-path copies; immutable PAC assets use hard links."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

ROOT = Path(__file__).resolve().parent.parent
EXPECTED = '05b14b047e49dabb026c96150f8d5aab497d20d9b2d52f5e59a8aef1af5b8dc0'

def prepare(name):
    if name not in ('A', 'B', 'C'):
        raise ValueError('Only isolated A/B/C test directories are supported')
    source = ROOT / 'runtime/app'
    target = Path(tempfile.gettempdir()) / 'rbo-netplay-20261108' / name
    target.mkdir(parents=True, exist_ok=True)
    assert str(target).isascii()
    for p in source.glob('*'):
        if p.is_file() and p.suffix.lower() in ('.exe', '.bmp'):
            dest = target / p.name
            if not dest.exists():
                shutil.copy2(p, dest)
    data = target / 'DATA'
    data.mkdir(exist_ok=True)
    for p in (source / 'DATA').glob('*'):
        if p.is_file() and p.suffix.lower() == '.pac':
            dest = data / p.name
            if not dest.exists():
                os.link(p, dest)
    for d in ('etc', 'SaveData', 'Movie'):
        (data / d).mkdir(exist_ok=True)
    path_record = str(target).encode('ascii') + b'\0'
    (data / 'etc/ex3_old_rbo_path.end').write_bytes(path_record.ljust(260, b'\0') * 3)
    (target / '_FCommand1st.Txt').write_text('BootScreenMode WINDOW\n',encoding='ascii')
    (target / '_FCommand.Txt').write_text('BootScreenMode WINDOW\n',encoding='ascii')
    exe = target / 'rbo_ex3.exe'
    assert hashlib.sha256(exe.read_bytes()).hexdigest() == EXPECTED
    return {'name':name, 'directory':str(target), 'exe':str(exe), 'sha256':EXPECTED}

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('instances', nargs='*', default=['A','B'])
    args = p.parse_args()
    result = [prepare(n) for n in args.instances]
    (ROOT / 'analysis/test-runtimes.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))
