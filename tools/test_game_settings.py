"""Folder preference regression tests; no game process is started."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import game_settings as settings


class GameSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.path = self.root / 'launcher/game-settings.json'
        self.default = self.root / 'launcher/runtime/app'
        self.game = self.make_game('게임 EX3')
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def make_game(self, name):
        folder = self.root / name
        (folder / 'DATA').mkdir(parents=True)
        (folder / 'rbo_ex3.exe').write_bytes(b'path validation fixture, never executed')
        return folder

    def pick(self, **kwargs):
        return settings.pick_game(settings_path=self.path, default=self.default, **kwargs)

    def save(self, folder=None):
        self.assertTrue(settings.save_game_settings(self.path, folder or self.game))

    def test_first_choice_remembered_and_next_run_does_not_prompt(self):
        with patch('builtins.input', return_value=f'"{self.game}"') as prompt:
            self.assertEqual(self.pick(), self.game)
            prompt.assert_called_once()
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')),
                         {'game_directory': str(self.game)})
        with patch('builtins.input', side_effect=AssertionError('must not ask again')):
            self.assertEqual(self.pick(), self.game)

    def test_executable_path_is_accepted(self):
        with patch('builtins.input', return_value=f'"{self.game / "rbo_ex3.exe"}"'):
            self.assertEqual(self.pick(), self.game)
        self.assertEqual(settings.read_game_settings(self.path), self.game)

    def test_relative_input_stored_absolute_and_survives_working_directory_change(self):
        original = Path.cwd()
        try:
            os.chdir(self.root)
            with patch('builtins.input', return_value=self.game.name):
                self.assertEqual(self.pick(), self.game)
            os.chdir(self.root.parent)
            with patch('builtins.input', side_effect=AssertionError('must use absolute path')):
                self.assertEqual(self.pick(), self.game)
        finally:
            os.chdir(original)

    def test_saved_selection_takes_precedence_over_bundled_game(self):
        self.make_game('launcher/runtime/app')
        self.save()
        with patch('builtins.input', side_effect=AssertionError('must use saved selection')):
            self.assertEqual(self.pick(), self.game)

    def test_bundled_game_still_works_without_prompt(self):
        self.make_game('launcher/runtime/app')
        with patch('builtins.input', side_effect=AssertionError('must use bundled game')):
            self.assertEqual(self.pick(), self.default)

    def test_change_game_and_keep_selection(self):
        other = self.make_game('다른 게임')
        self.save()
        with patch('builtins.input', return_value=str(other)):
            self.assertEqual(self.pick(force=True), other)
        with patch('builtins.input', return_value=''):
            self.assertEqual(self.pick(force=True), other)
        self.assertEqual(settings.read_game_settings(self.path), other)

    def test_moved_game_is_requested_again_and_updated(self):
        self.save()
        moved = self.root / '옮긴 게임'
        self.game.rename(moved)
        with patch('builtins.input', return_value=str(moved)):
            self.assertEqual(self.pick(), moved)
        self.assertEqual(settings.read_game_settings(self.path), moved)

    def test_incomplete_or_empty_selection_retries_without_saving(self):
        incomplete = self.root / 'missing-data'
        incomplete.mkdir()
        (incomplete / 'rbo_ex3.exe').touch()
        def answer(prompt):
            self.assertFalse(self.path.exists())
            return next(answers)
        answers = iter(['', str(self.root / 'missing-game'), str(incomplete), str(self.game)])
        with patch('builtins.input', side_effect=answer) as prompt:
            self.assertEqual(self.pick(), self.game)
            self.assertEqual(prompt.call_count, 4)

    def test_corrupt_settings_fall_back_to_selection(self):
        for data in (b'not-json', b'[]', b'{}', b'{"game_directory":7}',
                     b'{"game_directory":"relative"}', b'\xff', b' ' * 65537):
            with self.subTest(data=data[:40]):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_bytes(data)
                with patch('builtins.input', return_value=str(self.game)):
                    self.assertEqual(self.pick(), self.game)

    def test_unreadable_settings_do_not_abort_selection(self):
        self.path.mkdir(parents=True)
        with patch('builtins.input', return_value=str(self.game)):
            self.assertEqual(self.pick(), self.game)
        self.assertIn('기억하지 못했습니다', self.output.getvalue())

    def test_failed_atomic_replace_preserves_previous_choice_and_cleans_temp(self):
        self.save()
        original = self.path.read_bytes()
        other = self.make_game('새 게임')
        with patch.object(settings.os, 'replace', side_effect=PermissionError('read only')):
            with patch('builtins.input', return_value=str(other)):
                with self.assertRaises(OSError):
                    self.pick(force=True)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])
        self.assertIn('기억하지 못했습니다', self.output.getvalue())

    def test_cancel_keeps_previous_choice_and_does_not_touch_saves_or_endpoint(self):
        self.save()
        protected = (self.path, self.path.parent / 'join-settings.json',
                     self.path.parent / 'saves/host/current.json', self.game / 'DATA/KeyConfig.v4')
        for path in protected[1:]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'preserve this content')
        before = {path: path.read_bytes() for path in protected}
        with patch('builtins.input', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.pick(force=True)
        self.assertEqual({path: path.read_bytes() for path in protected}, before)

    def test_selection_cli_survives_a_fresh_process(self):
        tools = self.path.parent / 'tools'
        tools.mkdir(parents=True)
        module = tools / 'game_settings.py'
        shutil.copyfile(settings.__file__, module)
        selected = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(module)],
                                  input=str(self.game) + '\n', capture_output=True,
                                  encoding='utf-8', timeout=15)
        self.assertEqual(selected.returncode, 0, selected.stdout + selected.stderr)
        reused = subprocess.run([sys.executable, '-B', '-X', 'utf8', '-c',
                                 'import sys; sys.path.insert(0, "tools"); '
                                 'from game_settings import pick_game; print(pick_game())'],
                                cwd=self.path.parent, stdin=subprocess.DEVNULL,
                                capture_output=True, encoding='utf-8', timeout=15)
        self.assertEqual(reused.returncode, 0, reused.stdout + reused.stderr)
        self.assertIn(str(self.game), reused.stdout)

    def test_all_interactive_entrypoints_use_saved_folder_without_prompt(self):
        import netplay_launcher
        import singleplayer
        import netplay_config
        self.save()
        class BeforeGame(Exception):
            pass
        with patch.object(settings, 'ROOT', self.path.parent):
            with patch('builtins.input', side_effect=AssertionError('must not ask again')):
                for mode in ('join', 'host'):
                    with self.subTest(mode=mode), patch.object(sys, 'argv', ['launcher', mode, '--players', '2']):
                        with patch.object(netplay_launcher, 'fingerprint', side_effect=BeforeGame) as probe:
                            with self.assertRaises(BeforeGame):
                                netplay_launcher.main()
                            probe.assert_called_once_with(self.game)
                with patch.object(sys, 'argv', ['single']):
                    with patch.object(singleplayer, 'play_single') as play:
                        singleplayer.main()
                        play.assert_called_once_with(self.game)
                with patch.object(sys, 'argv', ['settings', '--build-only']):
                    with patch.object(netplay_config, 'build') as build:
                        netplay_config.main()
                        build.assert_called_once_with(self.game)


if __name__ == '__main__':
    unittest.main()
