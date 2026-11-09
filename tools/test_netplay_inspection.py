"""Only proven presentation differences may be waived; gameplay stays exact."""
import copy
import hashlib
import struct
import tempfile
import unittest
from pathlib import Path

from netplay_inspection import classify
from netplay_game import capture_state, PRESENTATION_FIELDS
from test_entity_commands import Memory
from netplay_saves import materialize
from test_resumable_transport import SAVE


class InspectionTests(unittest.TestCase):
    def setUp(self):
        self.row = {'scene': 0, 'next_scene': 0, 'mode': 1, 'stage': 0,
                    'characters': 'a' * 64, 'rng': [1, 2, 3],
                    'combat': {'state': 'a' * 64}, 'presentation': 'b' * 64}

    def inspect(self, changed, different=True, stage=None):
        return classify({0: self.row, 1: changed}, {0: 'a' * 64, 1: ('b' if different else 'a') * 64},
                        {'stage': 0} if stage is None else stage)

    def test_presentation_difference_continues_only_with_equal_simulation(self):
        changed = dict(self.row, presentation='c' * 64)
        self.assertEqual(self.inspect(changed, False),
                         {'kind': 'presentation', 'fields': ['presentation'], 'action': 'continue'})
        self.assertNotEqual(self.inspect(changed)['action'], 'continue')

    def test_even_one_random_step_requires_recovery(self):
        changed = dict(self.row, rng=[1, 3, 3])
        self.assertEqual(self.inspect(changed), {'kind': 'random', 'fields': ['rng'], 'action': 'reload'})

    def test_character_change_requires_recovery(self):
        changed = dict(self.row, characters='c' * 64)
        self.assertEqual(self.inspect(changed)['kind'], 'characters')
        self.assertEqual(self.inspect(changed)['action'], 'reload')

    def test_unknown_difference_is_never_accepted(self):
        changed = dict(self.row, unknown=1)
        self.assertEqual(self.inspect(changed)['kind'], 'unknown')
        self.assertNotEqual(self.inspect(changed)['action'], 'continue')
        self.assertNotEqual(self.inspect(self.row)['action'], 'continue')

    def test_menu_transition_or_different_stage_requires_restart(self):
        for changed in (dict(self.row, scene=4), dict(self.row, next_scene=4),
                        dict(self.row, mode=2), dict(self.row, stage=1), dict(self.row, combat=None)):
            self.assertEqual(self.inspect(changed)['action'], 'restart')
        self.assertEqual(classify({0: self.row, 1: self.row}, {0: 'a', 1: 'b'}, None)['action'], 'restart')

    def test_native_presentation_fields_change_diagnostic_only(self):
        original = Memory()
        baseline = capture_state(original)
        for offset in PRESENTATION_FIELDS:
            changed = Memory()
            struct.pack_into('<I', changed.blob, offset, 12345)
            before, after = hashlib.sha256(), hashlib.sha256()
            capture_state(original, presentation=before)
            self.assertEqual(baseline, capture_state(changed, presentation=after))
            self.assertNotEqual(before.digest(), after.digest())

    def test_unidentified_entity_byte_stays_in_simulation_hash(self):
        original, changed = Memory(), Memory()
        changed.blob[0x100] ^= 1
        self.assertNotEqual(capture_state(original)['state'], capture_state(changed)['state'])

    def test_classification_does_not_mutate_evidence(self):
        changed = dict(self.row, rng=[9, 2, 3])
        before = copy.deepcopy((self.row, changed))
        self.inspect(changed)
        self.assertEqual(before, (self.row, changed))

    def test_private_reload_discards_unconfirmed_saves_but_keeps_preferences(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            materialize(SAVE, directory)
            originals = {name: (directory / name).read_bytes() for name in SAVE['files']}
            (directory / 'chr_data01.sv4').write_bytes(b'unconfirmed')
            (directory / 'chr_data04.sv4').write_bytes(b'unconfirmed new slot')
            (directory / 'KeyConfig.v4').write_bytes(b'local keys')
            (directory / 'SystemConfig.v4').write_bytes(b'local settings')
            materialize(SAVE, directory, discard_unconfirmed=True)
            self.assertEqual(originals, {name: (directory / name).read_bytes() for name in originals})
            self.assertFalse((directory / 'chr_data04.sv4').exists())
            self.assertEqual((directory / 'KeyConfig.v4').read_bytes(), b'local keys')
            self.assertEqual((directory / 'SystemConfig.v4').read_bytes(), b'local settings')


if __name__ == '__main__':
    unittest.main()
