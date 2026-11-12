"""Verify the compiled localization against the owner's original game files."""
import base64
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parent))
from netplay_korean import ROOT, PACK, install, resource_index, read_resource
from korean_validation import controls, validate_catalog


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
                self.assertEqual(controls(original[start:start + size]),
                                 controls(changed[start:start + size]), name)
                _, padding = changed[start:start + size].split(b'\0', 1)
                self.assertFalse(any(padding), name)
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
            old_table = struct.unpack_from('<32512H', original, prefix + 8)
            for before, after in zip(old_table, table):
                if before != after:
                    self.assertEqual(before, 0xffff, name)
                    self.assertTrue(count <= after < count + added, name)
            new_entries = [v for v in table if count <= v < count + added]
            self.assertEqual(sorted(new_entries), list(range(count, count + added)), name)
            self.assertTrue(any(changed[end:end + added * glyph_size]), name)
            if prefix:
                self.assertEqual(struct.unpack_from('<I', changed, 2)[0],
                                 struct.unpack_from('<I', original, 2)[0] + added * glyph_size)

    def test_all_script_headers_catalog_and_resource_paths(self):
        scripts = {name: read_resource(self.index, name) for name in self.index
                   if name.endswith('.FOB')}
        locations = json.loads((ROOT / 'localization/ko/locations.json').read_bytes())
        validate_catalog(scripts, locations, self.index)
        # Independently preserve resource filenames everywhere in patched scripts,
        # including references omitted from the translation inventory.
        names = [name.encode('cp932') + b'\0' for name in self.index]
        for name, original in scripts.items():
            path = self.target / 'korean' / name
            if not path.exists():
                continue
            changed = path.read_bytes()
            for resource in names:
                start = 0
                while (start := original.upper().find(resource.upper(), start)) >= 0:
                    self.assertEqual(changed[start:start + len(resource)],
                                     original[start:start + len(resource)], (name, start))
                    start += len(resource)

    def test_script_function_names_and_references_are_unchanged(self):
        # Japanese text can be a cross-script callback identifier, not dialogue.
        # Check every occurrence, independently of the translation catalog.
        scripts = {name: read_resource(self.index, name) for name in self.index
                   if name.endswith('.FOB')}
        symbols = set()
        for name, original in scripts.items():
            count = struct.unpack_from('<I', original)[0]
            self.assertLessEqual(4 + count * 36, len(original), name)
            symbols.update(original[4 + i * 36:36 + i * 36].split(b'\0')[0]
                           for i in range(count))
        for name, original in scripts.items():
            path = self.target / 'korean' / name
            if not path.exists():
                continue
            changed = path.read_bytes()
            for symbol in symbols - {b''}:
                start = 0
                while (start := original.find(symbol + b'\0', start)) >= 0:
                    with self.subTest(file=name, offset=start, symbol=symbol):
                        self.assertEqual(changed[start:start + len(symbol) + 1], symbol + b'\0')
                    start += len(symbol) + 1

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

    def test_ui_changes_stay_inside_declared_label_boxes(self):
        labels = json.loads((ROOT / 'localization/ko/ui.json').read_bytes())
        for name, rows in labels.items():
            original = read_resource(self.index, name)
            restored = bytearray((self.target / 'korean' / name).read_bytes())
            width, height = struct.unpack_from('<II', original, 12)
            for row in rows:
                x, y, w, h = row['box']
                self.assertTrue(0 <= x < x + w <= width and 0 <= y < y + h <= height, name)
                for line in range(y, y + h):
                    start = 20 + (line * width + x) * 4
                    restored[start:start + w * 4] = original[start:start + w * 4]
            self.assertEqual(restored, original, name)


if __name__ == '__main__':
    unittest.main()
