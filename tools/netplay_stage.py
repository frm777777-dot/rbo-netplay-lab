"""Portable stage-entry data only; never restores process pointers or handles."""
import base64
import struct


def validate_stage(value, players):
    if type(value) is not dict or set(value) != {'format', 'stage', 'characters', 'slots', 'rng'}:
        raise ValueError('Invalid stage checkpoint fields')
    if value['format'] != 1 or type(value['stage']) is not int or not 0 <= value['stage'] < 15:
        raise ValueError('Unsupported campaign stage')
    try:
        characters = base64.b64decode(value['characters'], validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError('Invalid stage characters') from exc
    if len(characters) != 0x660 * 3:
        raise ValueError('Invalid stage character size')
    slots = value['slots']
    rng = value['rng']
    if (players not in (2, 3) or type(slots) is not list or len(slots) != 3 or
            not all(type(v) is int and 0 <= v < 30 for v in slots[:players]) or
            not all(type(v) is int and (0 <= v < 30 or v == 0xffffffff) for v in slots[players:])):
        raise ValueError('Invalid stage save slots')
    if len(set(slots[:players])) != players:
        raise ValueError('Duplicate active character save slots')
    if type(rng) is not list or len(rng) != 3 or not all(type(v) is int and 0 <= v <= 0x7fffffff for v in rng):
        raise ValueError('Invalid stage random streams')
    jobs = [struct.unpack_from('<i', characters, i * 0x660)[0] for i in range(3)]
    if not all(0 <= v < 14 for v in jobs[:players]) or not all(v == -1 for v in jobs[players:]):
        raise ValueError('Stage party does not match room size')
    return characters


def capture_stage(debugger, players):
    value = {'format': 1, 'stage': debugger.uint(0xb07948),
             'characters': base64.b64encode(debugger.read(0x25109c8, 0x660 * 3)).decode('ascii'),
             'slots': debugger.words(0x251008c, 3), 'rng': debugger.words(0x2558028, 3)}
    validate_stage(value, players)
    return value


def restore_stage(debugger, value, players):
    debugger.write(0x25109c8, validate_stage(value, players))
    debugger.write(0x251008c, struct.pack('<3I', *value['slots']))
    debugger.write(0x2558028, struct.pack('<3I', *value['rng']))
    debugger.write(0xadeb2c, struct.pack('<I', 1))
    debugger.write(0xb064b4, bytes(4))
    debugger.write(0xb07948, struct.pack('<I', value['stage']))
    debugger.write(0x2553c20, struct.pack('<I', value['stage']))
    debugger.write(0x4a5e78, struct.pack('<I', 2))
    debugger.write(0x4a6490, struct.pack('<4I', players, *[int(i < players) for i in range(3)]))
    for i, slot in enumerate(value['slots']):
        debugger.write(0x4a2920 + i * 0x1018 + 0x4c, struct.pack('<I', slot))
