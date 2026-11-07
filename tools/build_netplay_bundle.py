"""Package bridge source and the signed official Python runtime, without RBO assets."""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent.parent
FILES = ('Host-Online.cmd', 'Join-Online.cmd', '온라인 시험판 사용법.txt',
         'tools/netplay_launcher.py', 'tools/netplay_game.py',
         'tools/netplay_campaign.py', 'tools/netplay_saves.py',
         'tools/netplay_protocol.py', 'tools/win32_debug.py',
         'runtime/python-source.json')


def main():
    output = ROOT / 'dist/RBO-Netplay-Lab.zip'
    output.parent.mkdir(exist_ok=True)
    files = [ROOT / name for name in FILES]
    files += [p for p in (ROOT / 'runtime/python').rglob('*') if p.is_file() and '__pycache__' not in p.parts]
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(files):
            archive.write(path, 'RBO-Netplay-Lab/' + path.relative_to(ROOT).as_posix())
    manifest = {'package': output.name, 'size': output.stat().st_size,
                'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
                'contains_game_files': False,
                'files': [str(p.relative_to(ROOT)) for p in files]}
    (output.parent / 'bundle-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in manifest.items() if k != 'files'}, indent=2))


if __name__ == '__main__':
    main()
