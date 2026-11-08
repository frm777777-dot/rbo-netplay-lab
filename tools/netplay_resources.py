"""Canonical EX3 script references, identified by full bytecode and offset.

Native loader 0x427ed7/0x428025 stores bytecode pointer/length in the
64 descriptors at 0xaadce0, stride 0x120, offsets +0x104/+0x108.
The interpreter 0x42bf9a resolves its program counter relative to that base.
Read only declared allocations; never sample bytes beyond a short command.
Instances last one frame: dynamic script changes cannot be hidden by a cache.
"""
import hashlib
import struct

from netplay_protocol import require


class ScriptResources:
    def __init__(self, debugger):
        self.debugger = debugger
        table = debugger.read(0xaadce0, 64 * 0x120)
        self.ranges = []
        self.digests = {}
        total = 0
        for slot in range(64):
            base, size = struct.unpack_from('<2I', table, slot * 0x120 + 0x104)
            if base:
                require(2 <= size <= 4 * 1024 * 1024 and base + size <= 0x100000000,
                        'Invalid script allocation')
                total += size
                require(total <= 32 * 1024 * 1024, 'Unexpected script memory size')
                self.ranges.append((base, size))
        self.ranges.sort()
        require(all(a + n <= b for (a, n), (b, _) in zip(self.ranges, self.ranges[1:])),
                'Overlapping script allocations')

    def reference(self, pointer):
        for base, size in self.ranges:
            if base <= pointer < base + size:
                if base not in self.digests:
                    self.digests[base] = hashlib.sha256(self.debugger.read(base, size)).digest()
                return self.digests[base] + struct.pack('<II', size, pointer - base)
        require(False, f'Command outside registered script data: {pointer:08x}')
