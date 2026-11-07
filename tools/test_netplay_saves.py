import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

from netplay_saves import SaveStore, capture, materialize, validate


class SaveTests(unittest.TestCase):
    def test_three_characters_round_trip_and_previous_revision_survives(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            store = SaveStore(root / 'host')
            initial = store.load()
            source = root / 'source'; source.mkdir()
            for slot in range(1, 4):
                (source / f'chr_data{slot:02}.sv4').write_bytes(b'\0\0\x03\0' + bytes([slot]) * 1640)
            checkpoint = capture(source, initial['profile'], 1)
            store.commit(checkpoint)
            replica = SaveStore(root / 'friend')
            replica.commit(checkpoint)
            materialize(replica.load(), root / 'runtime')
            self.assertEqual(store.load(), replica.load())
            for file in source.iterdir():
                self.assertEqual(file.read_bytes(), (root / 'runtime' / file.name).read_bytes())
            self.assertEqual(len(list((root / 'host/history').glob('*.json'))), 2)
            self.assertFalse((root / 'host/current.tmp').exists())

    def test_untrusted_names_sizes_and_encoding_cannot_write_files(self):
        empty = {'format': 1, 'profile': 'a' * 32, 'revision': 0, 'files': {}}
        for name, content in [('../escape.sv4', 'AA=='), ('KeyConfig.v4', 'AA=='),
                              ('chr_data00.sv4', 'AA=='), ('chr_data01.sv4', '!!!'),
                              ('chr_data01.sv4', base64.b64encode(bytes(1644)).decode())]:
            value = copy.deepcopy(empty); value['files'][name] = content
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate(value)

    def test_profile_conflict_and_rollback_preserve_current(self):
        with tempfile.TemporaryDirectory() as root:
            store = SaveStore(root)
            current = store.load(); current['revision'] = 2
            store.commit(current)
            original = store.current.read_bytes()
            for change in [{'profile': '0' * 32}, {'revision': 1}, {'files': {'SystemData.v4': base64.b64encode(bytes(92)).decode()}}]:
                invalid = dict(current, **change)
                with self.assertRaises(ValueError):store.commit(invalid)
                self.assertEqual(store.current.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
