"""Native single-player creation/reload and strict save separation checks."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import struct
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from singleplayer import ROOT, play_single, single_store
from netplay_protocol import ProtocolError
from netplay_saves import SaveStore, validate


def hashes(directory):
    return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.rglob('*') if p.is_file()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--game', type=Path, default=ROOT / 'runtime/app')
    parser.add_argument('--tag', default='single-create-reload')
    args = parser.parse_args()
    output = ROOT / 'analysis' / args.tag
    output.mkdir(parents=True, exist_ok=False)
    # Seed separate host/replica stores. These records must never enter single
    # play or be rewritten, even though they are siblings of its save folder.
    host = SaveStore(output / 'saves/host')
    online = host.load()
    online['revision'] += 1
    online['files']['chr_data03.sv4'] = base64.b64encode(b'\0\0\x03\0' + bytes(1640)).decode('ascii')
    host.commit(online)
    replica = SaveStore(output / 'saves/replicas' / online['profile'])
    replica.commit(online)
    preserved = {str(p): hashes(p) for p in (host.directory, replica.directory, args.game / 'DATA/SaveData')}
    with single_store(output) as store:
        fresh = store.load()
        assert not fresh['files'] and fresh['profile'] != online['profile']
        try:
            with single_store(output):
                raise AssertionError('Duplicate single save writer was accepted')
        except ProtocolError:
            pass
    summaries = []
    for run, frames in enumerate((3600, 2400)):
        before = SaveStore(output / 'saves/single').load()
        scenes, stages, phases, jobs = set(), set(), set(), set()
        selected_bytes = []

        def control(frame, meta, d):
            scenes.add(meta['scene'])
            # Try pressing 2P/3P join on every tick; the single bridge must
            # remove these inputs before the native menu consumes them.
            pointer = d.uint(0xbc80c4)
            d.write(pointer + 4, struct.pack('<2I', 16, 16))
            if meta['scene'] == 0:
                stages.add(meta['stage'] + 1)
                jobs.add(tuple(meta['jobs']))
                assert meta['jobs'][0] >= 0 and meta['jobs'][1:] == [-1, -1], meta
                if not selected_bytes:
                    selected_bytes.append(d.read(0x25109c8, 0x660).hex())
                return 16 if frame % 60 < 15 else 0
            if meta['scene'] == 4:
                phase = meta['selection'][0][0]
                phases.add(phase)
                if phase == 4:
                    return (16 if frame % 600 < 480 else 256) if frame % 60 < 15 else 0
            return 16 if frame % 60 < 15 else 0

        with patch('socket.create_connection', side_effect=AssertionError('Single opened a connection')), \
             patch('socket.socket', side_effect=AssertionError('Single opened a socket')):
            result = play_single(args.game, output, frames=frames, seconds=180, control=control)
        after = SaveStore(output / 'saves/single').load()
        assert result['completed'] and 1 in stages and after['profile'] == before['profile'], result
        assert 'chr_data01.sv4' in after['files'] and 'chr_data03.sv4' not in after['files']
        assert all(hashes(Path(p)) == values for p, values in preserved.items()), 'Other save data changed'
        if run:
            assert 3 not in phases and 4 not in phases, 'Existing character was recreated instead of loaded'
            assert selected_bytes == summaries[0]['first_character'], 'Reloaded character differs'
        else:
            assert 3 in phases and 4 in phases, 'Did not exercise character creation'
        summaries.append({'result': result, 'scenes': sorted(scenes), 'stages': sorted(stages),
                          'selection_phases': sorted(phases), 'party_jobs': sorted(jobs),
                          'save_revision': after['revision'], 'first_character': selected_bytes})
        validate(after)
        print('SINGLE_TEST_RUN', run + 1, 'PASS', sorted(stages), sorted(phases), flush=True)
    logs = sorted((output / 'logs').glob('*-single-*'))
    assert len(logs) == 2
    for log in logs:
        assert 'KOREAN_ASSETS 138' in (log / 'bridge.txt').read_text(encoding='utf-8')
        runtime = Path((log / 'runtime.txt').read_text(encoding='utf-8'))
        assert (runtime / 'korean/manifest.json').is_file()
    report = {'passed': True, 'network_unused': True, 'other_saves_unchanged': True,
              'separate_profiles': True, 'duplicate_writer_rejected': True,
              'runs': [{k: v for k, v in run.items() if k != 'first_character'} for run in summaries]}
    (output / 'summary.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
