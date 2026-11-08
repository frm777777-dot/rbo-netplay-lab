"""Relocated bow-command addresses must not hide actual simulation changes."""
import struct
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from netplay_game import capture_state
from netplay_protocol import ProtocolError


NODE = 0xbd3fc0


class Memory:
    def __init__(self, relocated=0, count=3):
        self.blob = bytearray(0x974)
        self.data = {0x25109c8: bytes(0x660 * 3), 0x2558028: bytes(12)}
        for group in range(6):
            self.data[0x22e84ec + group * 0x974] = struct.pack('<I', 0 if group else NODE)
        struct.pack_into('<I', self.blob, 0x6c0, NODE + 0x588)
        for index in range(count):
            offset = 0x588 + index * 8
            pointer = 0x4000000 + relocated + index * 0x100 + 2
            following = NODE + offset + 8 if index + 1 < count else 0
            struct.pack_into('<2I', self.blob, offset, following, pointer)
            self.data[pointer] = bytes([index + 1]) * 64
        self.data[NODE] = self.blob

    def read(self, address, size):
        for base, value in self.data.items():
            if base <= address and address + size <= base + len(value):
                return bytes(value[address - base:address - base + size])
        raise AssertionError(f'Unexpected read: {address:x}/{size}')

    def uint(self, address):
        return struct.unpack('<I', self.read(address, 4))[0]


class CommandTests(unittest.TestCase):
    def test_bow_third_pointer_relocation_preserves_state(self):
        self.assertEqual(capture_state(Memory()), capture_state(Memory(0x1000000)))

    def test_third_command_content_difference_is_detected(self):
        changed = Memory(0x1000000)
        pointer = struct.unpack_from('<I', changed.blob, 0x59c)[0]
        changed.data[pointer] = b'X' + changed.data[pointer][1:]
        self.assertNotEqual(capture_state(Memory())['state'], capture_state(changed)['state'])

    def test_entire_native_node_capacity_is_address_independent(self):
        for count in range(1, 36):
            with self.subTest(active_nodes=count):
                self.assertEqual(capture_state(Memory(count=count)), capture_state(Memory(0x1000000, count)))

    def test_every_command_slot_retains_content_validation(self):
        original = capture_state(Memory(count=35))['state']
        for index in range(35):
            with self.subTest(command_slot=index):
                changed = Memory(0x1000000, 35)
                pointer = struct.unpack_from('<I', changed.blob, 0x58c + index * 8)[0]
                changed.data[pointer] = changed.data[pointer][:-1] + b'X'
                self.assertNotEqual(original, capture_state(changed)['state'])

    def test_all_six_lists_are_address_independent(self):
        first, second = Memory(count=35), Memory(0x1000000, 35)
        for memory in (first, second):
            for group, head in enumerate(range(0x6a0, 0x6d0, 8)):
                indices = list(range(group, 35, 6))
                struct.pack_into('<I', memory.blob, head, NODE + 0x588 + indices[0] * 8)
                for position, index in enumerate(indices):
                    following = NODE + 0x588 + indices[position + 1] * 8 if position + 1 < len(indices) else 0
                    struct.pack_into('<I', memory.blob, 0x588 + index * 8, following)
        self.assertEqual(capture_state(first), capture_state(second))
        left, right = struct.unpack_from('<I', second.blob, 0x6a0)[0], struct.unpack_from('<I', second.blob, 0x6a8)[0]
        struct.pack_into('<I', second.blob, 0x6a0, right)
        struct.pack_into('<I', second.blob, 0x6a8, left)
        self.assertNotEqual(capture_state(first)['state'], capture_state(second)['state'])

    def test_inactive_pointer_is_not_dereferenced(self):
        changed = Memory()
        struct.pack_into('<I', changed.blob, 0x69c, 0xdeadbeef)
        self.assertEqual(capture_state(Memory()), capture_state(changed))

    def test_gameplay_value_still_changes_state(self):
        changed = Memory()
        struct.pack_into('<I', changed.blob, 0x98, 123)
        self.assertNotEqual(capture_state(Memory())['state'], capture_state(changed)['state'])

    def test_invalid_or_cyclic_list_is_rejected(self):
        for target in (NODE + 0x588, NODE + 0x580):
            changed = Memory()
            struct.pack_into('<I', changed.blob, 0x598, target)
            with self.assertRaises(ProtocolError):
                capture_state(changed)


if __name__ == '__main__':
    unittest.main()
