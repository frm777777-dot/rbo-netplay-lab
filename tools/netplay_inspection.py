"""Classify exact-frame evidence. Unknown differences are never waived."""
import json


def classify(details, digests, stage):
    rows = list(details.values())
    if not rows or not all(isinstance(row, dict) for row in rows):
        return {'kind': 'unknown', 'fields': [], 'action': 'restart'}
    fields = sorted(key for key in set().union(*(row.keys() for row in rows))
                    if len({json.dumps(row.get(key), sort_keys=True) for row in rows}) > 1)
    # Presentation evidence is intentionally separate from the simulation hash.
    # Equal simulation hashes alone permit continued play; no numeric epsilon
    # or unknown-memory exclusion is introduced here.
    if len(set(digests.values())) == 1:
        return {'kind': 'presentation' if fields == ['presentation'] else 'equal',
                'fields': fields, 'action': 'continue'}
    meaningful = [key for key in fields if key != 'presentation']
    labels = {'characters': 'characters', 'jobs': 'characters', 'rng': 'random',
              'combat': 'combat', 'pause': 'control', 'battle_phase': 'stage',
              'scene': 'scene', 'next_scene': 'scene', 'stage': 'stage', 'mode': 'scene'}
    kinds = sorted({labels.get(key, 'unknown') for key in meaningful})
    # Restrict native scene reload to a live normal battle with an accepted
    # checkpoint for this same stage. Menus, transition frames and invalid
    # memory captures retain the previous restart fallback.
    reloadable = stage is not None and all(
        row.get('scene') == 0 and row.get('next_scene') == 0 and row.get('mode') == 1
        and row.get('stage') == stage['stage'] and isinstance(row.get('combat'), dict)
        and isinstance(row['combat'].get('state'), str) and len(row['combat']['state']) == 64
        for row in rows)
    return {'kind': '+'.join(kinds) or 'unknown', 'fields': meaningful,
            'action': 'reload' if reloadable else 'restart'}
