"""Reject hazardous localization edits before compiling a distributable pack."""
import struct
import unittest

from korean_validation import (controls, script_header, validate_catalog,
                               validate_font_slots, validate_replacement)


def script(text, exported=()):
    head = struct.pack('<I', len(exported))
    for name in exported:
        head += name.encode('cp932').ljust(32, b'\0') + struct.pack('<I', 0)
    head += struct.pack('<I', 0)
    raw = text.encode('cp932') + b'\0'
    raw += b'\0' * (-len(raw) % 4)
    code = struct.pack('<HHI', 0, 1, len(raw) // 4) + raw + struct.pack('<HH', 4, 1)
    head += struct.pack('<I', len(code))
    return head + code, {'id': 'T', 'file': 'TEST.FOB', 'offset': len(head) + 8, 'capacity': len(raw) - 1}


class KoreanValidationTests(unittest.TestCase):
    def test_rejects_callback_but_accepts_visible_sentence(self):
        data, row = script('内部処理', ('内部処理',))
        with self.assertRaisesRegex(ValueError, 'Internal script function'):
            validate_catalog({'TEST.FOB': data}, [row])
        data, row = script('開始します', ('内部処理',))
        validate_catalog({'TEST.FOB': data}, [row])

    def test_rejects_paths_but_accepts_diagnostic_with_filename(self):
        for path in ('DATA\\敵.FOB', '音楽.WAV', 'C:\\DATA\\背景.IMG', 'DATA/custom.bin'):
            data, row = script(path)
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'Resource path'):
                validate_catalog({'TEST.FOB': data}, [row], ['CUSTOM.BIN'])
        data, row = script('エラー : Thiefbug.txt')
        validate_catalog({'TEST.FOB': data}, [row])

    def test_rejects_nonconstant_overlaps_and_bad_bounds(self):
        data, row = script('表示文字列')
        for modified in ({**row, 'offset': row['offset'] - 2},
                         {**row, 'capacity': 100000}, {**row, 'capacity': 3}):
            with self.subTest(row=modified), self.assertRaises(ValueError):
                validate_catalog({'TEST.FOB': data}, [modified])
        with self.assertRaisesRegex(ValueError, 'Overlapping'):
            validate_catalog({'TEST.FOB': data}, [row, row])

    def test_rejects_truncated_header_and_function_outside_script(self):
        data, _ = script('文字', ('callback',))
        for modified in (data[:-1], data[:8], data[:36] + struct.pack('<I', 0xfffffff0) + data[40:]):
            with self.assertRaises(ValueError):
                script_header(modified)

    def test_controls_are_characters_not_multibyte_trail_bytes(self):
        self.assertEqual(controls('表r'.encode('cp932')), [])
        self.assertEqual(controls(b'\xf0\x5cr'), [])
        self.assertEqual(controls(b'%d \\r \\xb10'), ['%d', '\\r', '\\xb10'])
        validate_replacement(b'%d text\\r', b'%d translated\\r', 30)
        for value in (b'translated\\r', b'%s translated\\r', b'%d translated', b'%d \0text\\r'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_replacement(b'%d text\\r', value, 30)
        with self.assertRaises(ValueError):
            validate_replacement(b'original', b'too long', 3)

    def test_new_glyphs_cannot_overwrite_used_slots(self):
        font = struct.pack('<HHHBB', 1, 1, 1, 8, 1) + b'\xff\xff' * 32512 + b'\x80'
        code = b'\xf0\x40'
        validate_font_slots(font, [code])
        data = bytearray(font)
        struct.pack_into('<H', data, 8 + (0xf040 - 0x8100) * 2, 0)
        with self.assertRaisesRegex(ValueError, 'existing character'):
            validate_font_slots(data, [code])
        with self.assertRaises(ValueError):
            validate_font_slots(font, [code, code])


if __name__ == '__main__':
    unittest.main()
