"""Local settings, safe session scope and failure isolation for game volume."""
import contextlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import game_audio as audio


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'audio-settings.json'

    def test_missing_setting_does_not_force_full_volume(self):
        self.assertIsNone(audio.read_volume(self.path))

    def test_saved_zero_middle_and_maximum_survive_reopening(self):
        for value in (0, 23, 100):
            audio.save_volume(value, self.path)
            self.assertEqual(audio.read_volume(self.path), value)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ['audio-settings.json'])

    def test_invalid_values_cannot_replace_previous_setting(self):
        audio.save_volume(23, self.path)
        for value in (-1, 101, True, 20.5, '30', None):
            with self.assertRaises(ValueError):
                audio.save_volume(value, self.path)
            self.assertEqual(audio.read_volume(self.path), 23)

    def test_corrupt_settings_never_turn_volume_up(self):
        for value in ('broken', '[]', '{"format":2,"volume":100}',
                      '{"format":1,"volume":true}', ' ' * 4097):
            self.path.write_text(value, encoding='utf-8')
            with self.assertRaises(ValueError):
                audio.read_volume(self.path)

    def test_failed_atomic_replace_keeps_old_setting(self):
        audio.save_volume(25, self.path)
        with patch.object(audio.os, 'replace', side_effect=OSError('read only')):
            with self.assertRaises(OSError):
                audio.save_volume(70, self.path)
        self.assertEqual(audio.read_volume(self.path), 25)
        self.assertEqual(len(list(self.path.parent.iterdir())), 1)

    def test_prompt_can_keep_existing_value(self):
        audio.save_volume(25, self.path)
        with patch('builtins.input', return_value=''), patch('builtins.print'):
            audio.configure_volume(self.path)
        self.assertEqual(audio.read_volume(self.path), 25)

    def test_live_reader_sharing_conflict_is_retried(self):
        audio.save_volume(25, self.path)
        replace = audio.os.replace
        calls = []
        def busy_then_replace(source, target):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError('sharing violation')
            replace(source, target)
        with patch.object(audio.os, 'replace', side_effect=busy_then_replace), patch.object(audio.time, 'sleep'):
            audio.save_volume(35, self.path)
        self.assertEqual(audio.read_volume(self.path), 35)
        self.assertEqual(len(calls), 3)

    def test_prompt_retries_invalid_input_and_saves(self):
        with patch('builtins.input', side_effect=['-1', '101', 'abc', '20.5', '0']), patch('builtins.print'):
            audio.configure_volume(self.path)
        self.assertEqual(audio.read_volume(self.path), 0)

    def test_saved_value_and_new_session_apply_without_resetting_unchanged_sessions(self):
        with patch.object(audio, 'process_sessions', return_value=iter([('old', 11), ('new', 22)])), \
                patch.object(audio, 'invoke', return_value=0) as call:
            seen = audio.apply_volume(None, 123, 25, {'old': 25})
        self.assertEqual(seen, {'old': 25, 'new': 25})
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.args[0:2], (22, 3))
        self.assertEqual(call.call_args.args[3], (.25, None))

    def test_setting_change_updates_all_owned_sessions(self):
        with patch.object(audio, 'process_sessions', return_value=iter([('a', 11), ('b', 22)])), \
                patch.object(audio, 'invoke', return_value=0) as call:
            audio.apply_volume(None, 123, 0, {'a': 50, 'b': 50})
        self.assertEqual(call.call_count, 2)
        self.assertTrue(all(v.args[3] == (0.0, None) for v in call.call_args_list))

    def test_silent_native_tests_never_start_mixer_worker(self):
        with patch.dict(os.environ, {'RBO_TEST_SILENT': '1'}), patch.object(audio, 'VolumeController') as worker:
            self.assertIsNone(audio.start_volume(123))
        worker.assert_not_called()

    def test_resaving_same_percentage_reapplies_after_manual_mixer_change(self):
        audio.save_volume(25, self.path)
        worker = audio.VolumeController(123, self.path)
        waits = []
        def wait(_):
            waits.append(1)
            if len(waits) == 1:
                stamp = self.path.stat().st_mtime_ns + 1000000
                os.utime(self.path, ns=(stamp, stamp))
            else:
                worker.stop.set()
        worker.stop.wait = wait
        with patch.object(audio, 'apartment', return_value=contextlib.nullcontext(None)), \
                patch.object(audio, 'apply_volume', return_value={'session': 25}) as apply:
            worker.run()
        self.assertEqual(apply.call_count, 2)
        self.assertTrue(all(call.args[3] == {} for call in apply.call_args_list))

    def test_worker_setup_failure_does_not_escape_into_game(self):
        with patch.dict(os.environ, {'RBO_TEST_SILENT': '0'}), \
                patch.object(audio, 'VolumeController', side_effect=OSError('not available')), patch('builtins.print'):
            self.assertIsNone(audio.start_volume(123))

    def test_corrupt_setting_does_not_apply_or_crash_worker(self):
        worker = audio.VolumeController(123, self.path)
        worker.stop.wait = lambda _: worker.stop.set()
        with patch.object(audio, 'apartment', return_value=contextlib.nullcontext(None)), \
                patch.object(audio, 'read_volume', side_effect=ValueError('broken')), \
                patch.object(audio, 'apply_volume') as apply, patch('builtins.print'):
            worker.run()
        apply.assert_not_called()


if __name__ == '__main__':
    unittest.main()
