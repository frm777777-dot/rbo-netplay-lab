"""Build-time safety checks for fixed-size RBO text replacements.

These checks reject known executable identifiers; they do not prove that an
arbitrary new string is dialogue. New catalog entries still need a usage review.
"""
import re
import struct


def script_header(data):
    """Validate the FOB header and return its bytecode start and exported names."""
    def word(offset):
        if offset < 0 or offset + 4 > len(data):
            raise ValueError('Truncated script header')
        return struct.unpack_from('<I', data, offset)[0]

    count = word(0)
    end = 4 + count * 36
    if end + 4 > len(data):
        raise ValueError('Invalid script export table')
    symbols = set()
    entries = []
    for offset in range(4, end, 36):
        name = data[offset:offset + 32]
        if b'\0' not in name:
            raise ValueError('Unterminated script export name')
        symbols.add(name.split(b'\0')[0])
        entries.append(word(offset + 32))
    arrays = word(end)
    pos = end + 4
    for _ in range(arrays):
        count = word(pos)
        pos += 4
        if pos + 4 * count > len(data):
            raise ValueError('Truncated script function array')
        entries.extend(word(pos + 4 * i) for i in range(count))
        pos += 4 * count
    size = word(pos)
    base = pos + 4
    if base + size != len(data):
        raise ValueError('Invalid script bytecode length')
    if any(pc != 0xffffffff and (pc % 2 or pc >= size) for pc in entries):
        raise ValueError('Invalid script function address')
    return base, symbols - {b''}


def controls(raw):
    # Decode first: a Japanese/custom glyph can contain 0x5c as its second
    # byte. That byte is not an ASCII backslash or a text control sequence.
    text = raw.split(b'\0')[0].decode('cp932')
    return re.findall(r'%[-+0 #]*\d*(?:\.\d+)?[a-zA-Z%]|\\+(?:r|x[bBtT][0-9]*)', text)


def validate_catalog(scripts, catalog, resource_names=()):
    headers = {name: script_header(data) for name, data in scripts.items()}
    symbols = set().union(*(item[1] for item in headers.values()))
    resources = {name.upper() for name in resource_names}
    intervals = {}
    for row in catalog:
        name, start, capacity = row['file'], row['offset'], row['capacity']
        data = scripts[name]
        base = headers[name][0]
        size = capacity + 1
        where = f'{name}:{start}'
        if start < base + 8 or start + size > len(data) or start % 2 or size <= 0:
            raise ValueError(f'Invalid translation bounds: {where}')
        # opcode 0/subtype 1 is an inline string constant, with a word count.
        if data[start - 8:start - 4] != b'\0\0\x01\0' or \
                struct.unpack_from('<I', data, start - 4)[0] * 4 != size:
            raise ValueError(f'Translation is not constant storage: {where}')
        raw = data[start:start + size]
        if b'\0' not in raw:
            raise ValueError(f'Unterminated original string: {where}')
        text = raw.split(b'\0')[0]
        if text in symbols:
            raise ValueError(f'Internal script function is not dialogue: {where}')
        decoded = text.decode('cp932')
        basename = decoded.replace('\\', '/').rsplit('/', 1)[-1].upper()
        if basename in resources or re.fullmatch(
                r'(?:[A-Za-z]:)?[^:\r\n<>|"*?]*\.(?:FOB|IMG|FNT|PAC|WAV|OGG|BMP|DAT|TXT|V4|SV4)', decoded, re.I):
            raise ValueError(f'Resource path is not dialogue: {where}')
        spans = intervals.setdefault(name, [])
        if any(start < end and begin < start + size for begin, end in spans):
            raise ValueError(f'Overlapping translation: {where}')
        spans.append((start, start + size))


def validate_replacement(original, encoded, capacity):
    if b'\0' in encoded or len(encoded) > capacity:
        raise ValueError('Translation exceeds storage or contains an embedded NUL')
    if controls(original) != controls(encoded):
        raise ValueError('Translation changes formatting or text controls')


def validate_font_slots(original, codes):
    prefix = 6 if original[:2] == b'\0\0' else 0
    count = struct.unpack_from('<H', original, prefix)[0]
    codes = list(codes)
    if count + len(codes) > 0xffff or len(set(codes)) != len(codes):
        raise ValueError('Too many or duplicate custom glyphs')
    for code in codes:
        index = int.from_bytes(code, 'big') - 0x8100
        if len(code) != 2 or not 0 <= index < 32512:
            raise ValueError('Custom glyph is outside the font table')
        if struct.unpack_from('<H', original, prefix + 8 + index * 2)[0] != 0xffff:
            raise ValueError('Custom glyph would replace an existing character')
