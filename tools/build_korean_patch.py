"""Compile our translations and glyphs into a delta-only pack (Pillow required).

Translation locations were identified from the owner's installed game.
No original texture, font file, script or English patch is included in the pack.
"""
import base64
import hashlib
import json
from pathlib import Path
import struct
import sys
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parent))
from PIL import Image, ImageDraw, ImageFont
from netplay_korean import ROOT, resource_index, read_resource

FONT = 'C:/Windows/Fonts/malgun.ttf'
BOLD = 'C:/Windows/Fonts/malgunbd.ttf'


def compressed(data):
    return base64.b64encode(zlib.compress(data, 9)).decode('ascii')


def glyph_bitmap(char, width, height):
    font = ImageFont.truetype(FONT, height)
    image = Image.new('L', (width, height))
    box = font.getbbox(char)
    ImageDraw.Draw(image).text(((width - (box[2] - box[0])) // 2 - box[0],
                               (height - (box[3] - box[1])) // 2 - box[1]), char, font=font, fill=255)
    return image


def extend_font(original, mapping):
    data = bytearray(original)
    prefix = 6 if original[:2] == b'\0\0' else 0
    count, width, height, ppb, bits = struct.unpack_from('<HHHBB', original, prefix)
    assert ppb * bits == 8 and 1 <= width <= 32 and 1 <= height <= 32
    stride = (width + ppb - 1) // ppb
    boundary = prefix + 8 + 0xfe00 + stride * height * count
    additions = bytearray()
    for i, (char, code) in enumerate(mapping.items()):
        struct.pack_into('<H', data, prefix + 8 + (int.from_bytes(code, 'big') - 0x8100) * 2, count + i)
        pixels = glyph_bitmap(char, width, height)
        for y in range(height):
            for x in range(0, width, ppb):
                byte = 0
                for k in range(ppb):
                    value = pixels.getpixel((x + k, y)) if x + k < width else 0
                    value = round(value * ((1 << bits) - 1) / 255)
                    byte |= value << (8 - bits * (k + 1))
                additions.append(byte)
    data[boundary:boundary] = additions
    struct.pack_into('<H', data, prefix, count + len(mapping))
    if prefix:
        offset = struct.unpack_from('<I', data, 2)[0]
        struct.pack_into('<I', data, 2, offset + len(additions))
    edits = []
    if prefix:
        edits.append((2, 4, bytes(data[2:6])))
    edits.append((prefix, 2, bytes(data[prefix:prefix + 2])))
    for i, code in enumerate(mapping.values()):
        pos = prefix + 8 + (int.from_bytes(code, 'big') - 0x8100) * 2
        edits.append((pos, 2, bytes(data[pos:pos + 2])))
    edits.append((boundary, 0, bytes(additions)))
    return edits


def render_label(label):
    x, y, w, h = label['box']
    background = tuple(label.get('background', [255, 255, 255, 0]))
    image = Image.new('RGBA', (w, h), background)
    draw = ImageDraw.Draw(image)
    lines = label['text'].split('\n')
    size = label.get('size', max(10, h - 3))
    while size >= 8:
        font = ImageFont.truetype(BOLD if label.get('bold', True) else FONT, size)
        boxes = [font.getbbox(line or ' ') for line in lines]
        widths = [b[2] - b[0] for b in boxes]
        line_height = size + 2
        if max(widths) <= w - 2 and line_height * len(lines) <= h + 2:
            break
        size -= 1
    assert size >= 8, (label['text'], label['box'])
    top = 0 if label.get('valign') == 'top' else (h - line_height * len(lines)) // 2
    for line, box, width in zip(lines, boxes, widths):
        left = (w - width) // 2 if label.get('align', 'left') == 'center' else 1
        draw.text((left - box[0], top - box[1] + 1), line, font=font,
                  fill=tuple(label.get('color', [255, 255, 255, 255])))
        top += line_height
    return image


def validate_dialogue_locations(index, catalog):
    """Reject executable callback names even when they contain Japanese text."""
    scripts = {name: read_resource(index, name) for name in index if name.endswith('.FOB')}
    symbols = set()
    for name, original in scripts.items():
        count = struct.unpack_from('<I', original)[0]
        if 4 + count * 36 > len(original):
            raise ValueError(f'Invalid script export table: {name}')
        symbols.update(original[4 + i * 36:36 + i * 36].split(b'\0')[0]
                       for i in range(count))
    for row in catalog:
        original = scripts[row['file']]
        start, size = row['offset'], row['capacity'] + 1
        text = original[start:start + size].split(b'\0')[0]
        if text in symbols:
            raise ValueError(f'Internal script function is not dialogue: {row["file"]}:{start}')


def main():
    source = ROOT / 'runtime/app'
    index = resource_index(source)
    catalog = json.loads((ROOT / 'localization/ko/locations.json').read_bytes())
    validate_dialogue_locations(index, catalog)
    translated = dict(line.split('\t', 1) for line in (ROOT / 'localization/ko/strings.tsv').read_text(encoding='utf-8').splitlines())
    assert set(translated) == {v['id'] for v in catalog}
    chars = sorted({c for text in translated.values() for c in text if '\uac00' <= c <= '\ud7a3' or '\u3131' <= c <= '\u318e'})
    codes = [bytes((lead, trail)) for lead in range(0xf0, 0xfd) for trail in range(0x40, 0xfd) if trail != 0x7f]
    assert len(chars) <= len(codes)
    mapping = dict(zip(chars, codes))

    def encode(text):
        return b''.join(mapping[c] if c in mapping else c.encode('cp932') for c in text)

    changes = {}
    for row in catalog:
        text = translated[row['id']]
        encoded = encode(text)
        assert len(encoded) <= row['capacity'], (row['file'], row['capacity'], len(encoded), text)
        changes.setdefault(row['file'], []).append((row['offset'], row['capacity'] + 1,
                                                   encoded.ljust(row['capacity'] + 1, b'\0')))
    for name in index:
        if name.endswith('.FNT'):
            changes[name] = extend_font(read_resource(index, name), mapping)
    ui_file = ROOT / 'localization/ko/ui.json'
    ui = json.loads(ui_file.read_bytes()) if ui_file.exists() else {}
    preview = ROOT / 'analysis/korean-previews'
    preview.mkdir(parents=True, exist_ok=True)
    for name, labels in ui.items():
        original = read_resource(index, name)
        version, kind, mode, width, height = struct.unpack_from('<5I', original)
        assert (version, kind, mode) == (0, 7, 2) and len(original) == 20 + width * height * 4
        image = Image.frombytes('RGBA', (width, height), original[20:], 'raw', 'BGRA')
        occupied = set()
        rows = {}
        for label in labels:
            x, y, w, h = label['box']
            assert 0 <= x < x + w <= width and 0 <= y < y + h <= height
            tile = render_label(label)
            image.paste(tile, (x, y))
            for line in range(y, y + h):
                for col in range(x, x + w):
                    occupied.add((line, col))
        # Merge adjacent modified pixels into row runs. Only replacement label
        # pixels are distributed; untouched artwork never enters the pack.
        for line, col in sorted(occupied):
            rows.setdefault(line, []).append(col)
        edits = changes.setdefault(name, [])
        for line, columns in rows.items():
            start = end = columns[0]
            for col in columns[1:] + [width + 1]:
                if col == end + 1:
                    end = col
                    continue
                raw = image.crop((start, line, end + 1, line + 1)).tobytes('raw', 'BGRA')
                edits.append((20 + (line * width + start) * 4, len(raw), raw))
                start = end = col
        image.save(preview / (Path(name).stem + '.png'))
    pack = {'format': 1, 'language': 'ko', 'glyphs': len(mapping), 'dialogue_occurrences': len(catalog),
            'ui_labels': sum(map(len, ui.values())), 'assets': {}}
    for name, edits in sorted(changes.items()):
        original = read_resource(index, name)
        edits.sort(key=lambda e: e[0])
        for a, b in zip(edits, edits[1:]):
            assert a[0] + a[1] <= b[0], (name, a[:2], b[:2])
        data = bytearray(original)
        for offset, removed, added in reversed(edits):
            data[offset:offset + removed] = added
        pack['assets'][name] = {'source': hashlib.sha256(original).hexdigest(),
                                'result': hashlib.sha256(data).hexdigest(),
                                'edits': [[offset, removed, compressed(added)] for offset, removed, added in edits]}
    (ROOT / 'localization/ko/patch.json').write_text(json.dumps(pack, separators=(',', ':')), encoding='ascii')
    (ROOT / 'analysis/korean-mapping.json').write_text(json.dumps({c: v.hex() for c, v in mapping.items()}, ensure_ascii=False), encoding='utf-8')
    print({k: v for k, v in pack.items() if k != 'assets'}, 'assets', len(pack['assets']))


if __name__ == '__main__':
    main()
