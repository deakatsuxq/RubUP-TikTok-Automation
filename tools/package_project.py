"""Build a source-only archive from an explicit allowlist; never package secrets."""
from pathlib import Path
import hashlib
import zipfile


def build(destination=None):
    root = Path(__file__).resolve().parents[1]
    target = Path(destination) if destination else root.parent / 'RubUP-TikTok-Automation.zip'
    target.parent.mkdir(parents=True, exist_ok=True)
    names = ['README.md', 'requirements.txt', 'config.yaml', '.env.example', '.gitignore',
             'pytest.ini', 'start.bat', 'build_windows.ps1',
             'images/.gitkeep', 'data/.gitkeep', 'logs/.gitkeep']
    files = [root / name for name in names if (root / name).is_file()]
    files += list(root.glob('*.py'))
    for folder in ('gui', 'tests', 'tools'):
        files += list((root / folder).glob('*.py'))
    files += list((root / 'docs').glob('*.md'))
    hashes = []
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(set(files)):
            relative = path.relative_to(root)
            raw = path.read_bytes()
            hashes.append(f'{hashlib.sha256(raw).hexdigest()}  {relative.as_posix()}')
            archive.writestr(f'{root.name}/{relative.as_posix()}', raw)
        archive.writestr(f'{root.name}/SHA256SUMS.txt', '\n'.join(hashes) + '\n')
    with zipfile.ZipFile(target) as archive:
        if archive.testzip() is not None:
            raise RuntimeError('ZIP integrity check failed')
    return target


if __name__ == '__main__':
    target = build()
    print(target)
    print('SHA256', hashlib.sha256(target.read_bytes()).hexdigest())
