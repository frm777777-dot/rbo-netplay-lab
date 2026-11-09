"""Bounded, validated native save checkpoints; host owns the online profile."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

MAX_CHECKPOINT_BYTES = 256 * 1024
SAVE_NAMES = {'SystemData.v4': 92, 'AreaRecordTime.rv4': 3484, 'ArenaInfo.rv4': 2812}
CHARACTER = re.compile(r'chr_data(0[1-9]|[12][0-9]|30)\.sv4\Z')


def expected_size(name):
    if type(name) is not str:
        return None
    return 1644 if CHARACTER.fullmatch(name) else SAVE_NAMES.get(name)


def encode(checkpoint):
    return json.dumps(checkpoint, sort_keys=True, separators=(',', ':')).encode('ascii')


def validate(checkpoint):
    if type(checkpoint) is not dict or set(checkpoint) != {'format', 'profile', 'revision', 'files'}:
        raise ValueError('Invalid save checkpoint fields')
    if checkpoint['format'] != 1 or type(checkpoint['format']) is not int:
        raise ValueError('Unsupported save format')
    if type(checkpoint['profile']) is not str or not re.fullmatch('[0-9a-f]{32}', checkpoint['profile']):
        raise ValueError('Invalid online save identity')
    if type(checkpoint['revision']) is not int or not 0 <= checkpoint['revision'] <= 10**12:
        raise ValueError('Invalid save revision')
    files = checkpoint['files']
    if type(files) is not dict or len(files) > 33 or len(encode(checkpoint)) > MAX_CHECKPOINT_BYTES:
        raise ValueError('Save checkpoint is too large')
    decoded = {}
    for name, content in files.items():
        size = expected_size(name)
        if size is None or type(content) is not str:
            raise ValueError('Unrecognized save file')
        try:
            blob = base64.b64decode(content, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError('Invalid save encoding') from exc
        if len(blob) != size:
            raise ValueError(f'Unexpected save size: {name}')
        if CHARACTER.fullmatch(name) and blob[:4] != b'\0\0\x03\0':
            raise ValueError('Unexpected character save version')
        decoded[name] = blob
    return decoded


def capture(directory, profile, revision):
    files = {}
    for path in sorted(Path(directory).iterdir()):
        if expected_size(path.name) is not None and path.is_file() and not path.is_symlink():
            files[path.name] = base64.b64encode(path.read_bytes()).decode('ascii')
    checkpoint = {'format': 1, 'profile': profile, 'revision': revision, 'files': files}
    validate(checkpoint)
    return checkpoint


def materialize(checkpoint, directory, *, discard_unconfirmed=False):
    """Write an owned runtime, before launch or while stopped at scene load."""
    blobs = validate(checkpoint)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if discard_unconfirmed:
        for path in directory.iterdir():
            if expected_size(path.name) is not None and path.name not in blobs:
                if path.is_symlink():
                    raise ValueError('Refusing a redirected save destination')
                if path.is_file():
                    path.unlink()
    for name, blob in blobs.items():
        path = directory / name
        if path.is_symlink():
            raise ValueError('Refusing a redirected save destination')
        path.write_bytes(blob)


class SaveStore:
    """Atomic current checkpoint plus immutable revision history, never game assets."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.current = self.directory / 'current.json'
        self.directory.mkdir(parents=True, exist_ok=True)

    def load(self):
        if not self.current.exists():
            checkpoint = {'format': 1, 'profile': uuid.uuid4().hex, 'revision': 0, 'files': {}}
            self.commit(checkpoint)
            return checkpoint
        checkpoint = json.loads(self.current.read_bytes())
        validate(checkpoint)
        return checkpoint

    def commit(self, checkpoint):
        validate(checkpoint)
        data = encode(checkpoint)
        if self.current.exists():
            previous = json.loads(self.current.read_bytes())
            validate(previous)
            if previous['profile'] != checkpoint['profile']:
                raise ValueError('Save profile changed unexpectedly')
            if checkpoint['revision'] < previous['revision']:
                raise ValueError('Refusing an older save revision')
            if checkpoint['revision'] == previous['revision']:
                if data != encode(previous):
                    raise ValueError('Conflicting save revision')
                return
        history = self.directory / 'history'
        history.mkdir(exist_ok=True)
        digest = hashlib.sha256(data).hexdigest()[:16]
        revision = history / f'{checkpoint["revision"]:08d}-{digest}.json'
        if not revision.exists():
            with revision.open('xb') as f:
                f.write(data); f.flush(); os.fsync(f.fileno())
        temporary = self.current.with_name('current.tmp')
        with temporary.open('wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(temporary, self.current)
