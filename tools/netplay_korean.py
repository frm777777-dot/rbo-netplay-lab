"""Apply the Korean delta pack to owned copies, never to the installed game."""
import base64
import hashlib
import json
from pathlib import Path
import struct
import zlib

from netplay_protocol import require

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / 'localization/ko/patch.json'
ORDER = ('bg01.pac', 'bg02.pac', 'cg.pac', 'data01.pac', 'data02.pac',
         'etc.pac', 'update01.pac', 'ex1disc.pac', 'ex2disc.pac', 'ex3disc.pac')


def resource_index(source):
    files = {p.name.lower(): p for p in (Path(source) / 'DATA').glob('*.pac')}
    result = {}
    for name in ORDER:
        if name not in files:
            continue
        path = files[name]
        with path.open('rb') as stream:
            version, count = struct.unpack('<II', stream.read(8))
            count ^= 0xe3df59ac
            require(version == 1 and count < 10000, '지원하지 않는 게임 자료 형식입니다.')
            table = stream.read(count * 68)
        for i in range(count):
            entry = table[i * 68:(i + 1) * 68]
            key = bytes(c ^ ((3 * i * j + 0x3d) & 255) for j, c in enumerate(entry[:59]))
            key = key.split(b'\0')[0].decode('cp932').upper()
            offset, size = struct.unpack_from('<II', entry, 60)
            size ^= 0xe3df59ac
            require(offset + size <= path.stat().st_size, '게임 자료가 손상되었습니다.')
            result[key] = (path, offset, size)
    return result


def read_resource(index, name):
    path, offset, size = index[name.upper()]
    with path.open('rb') as stream:
        stream.seek(offset)
        return stream.read(size)


def pack_hash():
    return hashlib.sha256(PACK.read_bytes()).hexdigest()


def install(source, target):
    pack = json.loads(PACK.read_bytes())
    require(pack['format'] == 1, '한글 패치 버전이 올바르지 않습니다.')
    index = resource_index(source)
    output = Path(target) / 'korean'
    output.mkdir(exist_ok=True)
    for name, change in pack['assets'].items():
        require(Path(name).name == name and '/' not in name and '\\' not in name,
                '한글 패치 파일명이 올바르지 않습니다.')
        original = read_resource(index, name)
        require(hashlib.sha256(original).hexdigest() == change['source'],
                f'한글 패치와 게임 자료 버전이 다릅니다: {name}')
        data = bytearray(original)
        for offset, removed, encoded in reversed(change['edits']):
            added = zlib.decompress(base64.b64decode(encoded, validate=True))
            require(0 <= offset <= len(original) and offset + removed <= len(original),
                    '한글 패치 범위가 올바르지 않습니다.')
            data[offset:offset + removed] = added
        require(hashlib.sha256(data).hexdigest() == change['result'], '한글 자료 검증에 실패했습니다.')
        (output / name).write_bytes(data)
    (output / 'manifest.json').write_text(json.dumps({'pack': pack_hash(), 'assets': sorted(pack['assets'])}), encoding='ascii')


def attach(debugger, directory, *, title='RBO EX3 - Korean Online'):
    """Redirect game-owned resource opens to localized copies of the same asset."""
    manifest = Path(directory) / 'korean/manifest.json'
    if not manifest.is_file():
        return
    assets = set(json.loads(manifest.read_bytes())['assets'])
    require(title.isascii() and len(title) < 40, 'Invalid game window title')
    for address in (0x497518, 0x497540):
        debugger.write(address, (title.encode('ascii') + b'\0').ljust(40, b'\0'))
    addresses = {}
    for name in assets:
        data = ('korean\\' + name).encode('ascii') + b'\0'
        address = debugger.allocate_data(len(data))
        debugger.write(address, data)
        addresses[name] = address

    def resource_open(d, context):
        pointer = d.uint(context.esp + 8)
        # The native file names are ordinary bounded ASCII paths. Read one
        # byte at a time so a terminator at a page boundary remains valid.
        raw = bytearray()
        for i in range(260):
            value = d.read(pointer + i, 1)
            if value == b'\0':
                break
            raw.extend(value)
        name = bytes(raw).replace(b'\\', b'/').rsplit(b'/', 1)[-1].decode('ascii', 'ignore').upper()
        if name in addresses:
            d.write(context.esp + 8, struct.pack('<I', addresses[name]))

    require(debugger.read(0x426fc0, 3) == bytes.fromhex('568b74'), '지원하지 않는 파일 로더입니다.')
    debugger.breakpoint(0x426fc0, resource_open)
    print('KOREAN_ASSETS', len(assets), flush=True)
