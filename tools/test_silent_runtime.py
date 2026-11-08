"""Mute configuration must be valid before a test game can be created."""
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from win32_debug import Debugger


class BeforeCreate(Exception):
    pass


class SilenceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.gettempdir()) / 'rbo-netplay-lab'
        self.root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=self.root, prefix='silent-unit-')
        self.directory = Path(self.temporary.name).resolve()
        assert self.directory.is_relative_to(self.root.resolve())
        self.config = self.directory / 'DATA/SaveData/SystemConfig.v4'
        self.config.parent.mkdir(parents=True)
        self.environment = patch.dict(os.environ, {'RBO_TEST_SILENT': '1'})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def initialize_without_launch(self):
        with patch('win32_debug.Create', side_effect=BeforeCreate) as create:
            with self.assertRaises(BeforeCreate):
                Debugger(self.directory / 'rbo_ex3.exe')
            self.assertEqual(create.call_count, 1)

    def test_missing_config_uses_valid_muted_defaults(self):
        self.initialize_without_launch()
        values = struct.unpack('<20I', self.config.read_bytes())
        self.assertEqual((values[0], values[15]), (3, 3))

    def test_existing_other_preferences_are_preserved(self):
        values = [3] + list(range(1, 20))
        self.config.write_bytes(struct.pack('<20I', *values))
        self.initialize_without_launch()
        values[15] = 3
        self.assertEqual(list(struct.unpack('<20I', self.config.read_bytes())), values)

    def test_bad_version_cancels_before_creation(self):
        self.config.write_bytes(bytes(80))
        with patch('win32_debug.Create') as create, self.assertRaises(ValueError):
            Debugger(self.directory / 'rbo_ex3.exe')
        create.assert_not_called()

    def test_movie_audio_cancels_before_creation(self):
        movie = self.directory / 'DATA/Movie'
        movie.mkdir()
        (movie / 'test.avi').write_bytes(b'test')
        with patch('win32_debug.Create') as create, self.assertRaises(ValueError):
            Debugger(self.directory / 'rbo_ex3.exe')
        create.assert_not_called()

    def test_original_location_cannot_be_modified_in_test_mode(self):
        with patch('win32_debug.Create') as create, self.assertRaises(ValueError):
            Debugger(Path(__file__).resolve().parent / 'rbo_ex3.exe')
        create.assert_not_called()


if __name__ == '__main__':
    unittest.main()
