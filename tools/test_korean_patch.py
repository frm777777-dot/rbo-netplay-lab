"""Verify the compiled localization against the owner's original game files."""
import base64
import hashlib
import json
from pathlib import Path
import re
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parent))
from netplay_korean import ROOT, PACK, install, resource_index, read_resource


class KoreanPatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = ROOT / 'runtime/app'
        cls.index = resource_index(cls.source)
        cls.pack = json.loads(PACK.read_bytes())
        cls.folder = tempfile.TemporaryDirectory(prefix='rbo-ko-test-')
        cls.target = Path(cls.folder.name)
        install(cls.source, cls.target)

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def test_scripts_change_only_constant_storage(self):
        locations = json.loads((ROOT / 'localization/ko/locations.json').read_bytes())
        grouped = {}
        for row in locations:
            grouped.setdefault(row['file'], []).append(row)
        for name, rows in grouped.items():
            original = read_resource(self.index, name)
            changed = (self.target / 'korean' / name).read_bytes()
            self.assertEqual(len(original), len(changed), name)
            restored = bytearray(changed)
            for row in rows:
                start, size = row['offset'], row['capacity'] + 1
                self.assertEqual(original[start - 6:start - 4], b'\x01\x00', name)
                self.assertEqual(struct.unpack_from('<I', original, start - 4)[0] * 4, size, name)
                self.assertIn(b'\0', changed[start:start + size], name)
                controls = rb'%[-+0 #]*\d*(?:\.\d+)?[a-zA-Z%]|\\+(?:r|x[bBtT][0-9]*)'
                self.assertEqual(re.findall(controls, original[start:start + size]),
                                 re.findall(controls, changed[start:start + size]), name)
                restored[start:start + size] = original[start:start + size]
            self.assertEqual(restored, original, name)

    def test_added_glyphs_preserve_original_bitmap_and_ascii(self):
        for name in self.pack['assets']:
            if not name.endswith('.FNT'):
                continue
            original = read_resource(self.index, name)
            changed = (self.target / 'korean' / name).read_bytes()
            prefix = 6 if original[:2] == b'\0\0' else 0
            count, width, height, ppb, bits = struct.unpack_from('<HHHBB', original, prefix)
            added = self.pack['glyphs']
            self.assertEqual(struct.unpack_from('<H', changed, prefix)[0], count + added)
            self.assertEqual(original[prefix + 2:prefix + 8], changed[prefix + 2:prefix + 8])
            bitmap = prefix + 8 + 0xfe00
            glyph_size = (width + ppb - 1) // ppb * height
            end = bitmap + count * glyph_size
            self.assertEqual(original[bitmap:end], changed[bitmap:end], name)
            self.assertEqual(original[end:], changed[end + added * glyph_size:], name)
            table = struct.unpack_from('<32512H', changed, prefix + 8)
            new_entries = [v for v in table if count <= v < count + added]
            self.assertEqual(sorted(new_entries), list(range(count, count + added)), name)
            self.assertTrue(any(changed[end:end + added * glyph_size]), name)
            if prefix:
                self.assertEqual(struct.unpack_from('<I', changed, 2)[0],
                                 struct.unpack_from('<I', original, 2)[0] + added * glyph_size)

    def test_images_preserve_dimensions_and_installed_originals(self):
        for name, change in self.pack['assets'].items():
            original = read_resource(self.index, name)
            changed = (self.target / 'korean' / name).read_bytes()
            self.assertEqual(hashlib.sha256(original).hexdigest(), change['source'])
            self.assertEqual(hashlib.sha256(changed).hexdigest(), change['result'])
            if name.endswith('.IMG'):
                self.assertEqual(original[:20], changed[:20], name)
                self.assertEqual(len(original), len(changed), name)
            # Pack operations contain only new labels/constants/font additions.
            for offset, removed, encoded in change['edits']:
                self.assertGreaterEqual(offset, 0)
                self.assertLessEqual(offset + removed, len(original))
                self.assertLess(len(zlib.decompress(base64.b64decode(encoded))), 4 * 1024 * 1024)


if __name__ == '__main__':
    unittest.main()
