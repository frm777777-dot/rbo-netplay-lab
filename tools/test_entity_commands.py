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
        table = bytearray(64 * 0x120)
        for index in range(count):
            pointer = 0x4000000 + relocated + index * 0x100 + 2
            struct.pack_into('<2I', table, index * 0x120 + 0x104, pointer, 64)
        self.data[0xaadce0] = table

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

    def test_all_inactive_node_words_are_irrelevant(self):
        expected = capture_state(Memory(count=3))
        for offset in range(0x5a0, 0x6a0, 4):
            changed = Memory(count=3)
            struct.pack_into('<I', changed.blob, offset, 0xdeadbeef)
            self.assertEqual(expected, capture_state(changed), hex(offset))

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

    def test_script_change_beyond_old_64_byte_prefix_is_detected(self):
        first, changed = Memory(count=1), Memory(0x1000000, count=1)
        for memory in (first, changed):
            pointer = struct.unpack_from('<I', memory.blob, 0x58c)[0]
            memory.data[pointer] += bytes(128)
            struct.pack_into('<I', memory.data[0xaadce0], 0x108, 192)
        pointer = struct.unpack_from('<I', changed.blob, 0x58c)[0]
        changed.data[pointer] = changed.data[pointer][:-1] + b'X'
        self.assertNotEqual(capture_state(first)['state'], capture_state(changed)['state'])

    def test_short_command_at_allocation_end_is_not_overread(self):
        first, second = Memory(count=1), Memory(0x1000000, count=1)
        for memory in (first, second):
            pointer = struct.unpack_from('<I', memory.blob, 0x58c)[0]
            memory.data[pointer] = bytes(4)
            struct.pack_into('<I', memory.data[0xaadce0], 0x108, 4)
        self.assertEqual(capture_state(first), capture_state(second))

    def test_script_offset_cannot_be_replaced_with_identical_neighbor(self):
        first, changed = Memory(count=1), Memory(count=1)
        pointer = struct.unpack_from('<I', changed.blob, 0x58c)[0]
        struct.pack_into('<I', changed.blob, 0x58c, pointer + 1)
        self.assertNotEqual(capture_state(first)['state'], capture_state(changed)['state'])

    def test_attack_damage_and_animation_records_are_compared(self):
        first, changed = Memory(), Memory()
        for memory in (first, changed):
            for offset, address, size in ((0x64, 0x6000000, 300), (0x57c, 0x6001000, 300),
                                          (0x76c, 0x6002000, 120), (0x770, 0x6003000, 8)):
                struct.pack_into('<I', memory.blob, offset, address)
                memory.data[address] = bytes(size)
            struct.pack_into('<I', memory.blob, 0x768, 1)
        expected = capture_state(first)['state']
        for address in (0x6000000, 0x6001000, 0x6002000, 0x6003000):
            previous = changed.data[address]
            changed.data[address] = previous[:-1] + b'X'
            self.assertNotEqual(expected, capture_state(changed)['state'])
            changed.data[address] = previous

    def test_all_three_body_rectangles_keep_content_not_addresses(self):
        first, changed = Memory(), Memory(0x1000000)
        for memory, relocated in ((first, 0), (changed, 0x1000)):
            struct.pack_into('<I', memory.blob, 0x78c, 3)
            for index in range(3):
                pointer = 0x7000000 + relocated + index * 8
                struct.pack_into('<I', memory.blob, 0x790 + index * 4, pointer)
                memory.data[pointer] = bytes(8)
        expected = capture_state(first)['state']
        self.assertEqual(expected, capture_state(changed)['state'])
        for index in range(3):
            pointer = 0x7001000 + index * 8
            changed.data[pointer] = b'X' + bytes(7)
            self.assertNotEqual(expected, capture_state(changed)['state'])
            changed.data[pointer] = bytes(8)


if __name__ == '__main__':
    unittest.main()
