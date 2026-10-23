"""Build a source-only archive from an explicit allowlist; never package secrets."""
from pathlib import Path
import hashlib
import os
import tempfile
import zipfile


def build(destination=None):
    root = Path(__file__).resolve().parents[1]
    target = Path(destination) if destination else root.parent / 'RubUP-Automation-3.0-Windows.zip'
    target.parent.mkdir(parents=True, exist_ok=True)
    names = ['README.md', 'requirements.txt', 'config.yaml', '.env.example', '.gitignore', '.gitattributes',
             'pytest.ini', 'start.bat', 'install.bat', 'setup_windows.ps1', 'build_windows.ps1',
             'images/.gitkeep', 'data/.gitkeep', 'logs/.gitkeep']
    files = [root / name for name in names if (root / name).is_file()]
    files += list(root.glob('*.py'))
    for folder in ('gui', 'tests', 'tools'):
        files += list((root / folder).glob('*.py'))
    files += list((root / 'docs').glob('*.md'))
    files += list((root / 'docs').glob('*.png'))
    hashes = []
    fd, temporary_name = tempfile.mkstemp(prefix='.rubup-release-', suffix='.zip', dir=target.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(set(files)):
                if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                    raise ValueError('Archive source must be a regular file inside the project')
                relative = path.relative_to(root)
                raw = path.read_bytes()
                hashes.append(f'{hashlib.sha256(raw).hexdigest()}  {relative.as_posix()}')
                archive.writestr(f'{root.name}/{relative.as_posix()}', raw)
            archive.writestr(f'{root.name}/SHA256SUMS.txt', '\n'.join(hashes) + '\n')
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise RuntimeError('ZIP integrity check failed')
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


if __name__ == '__main__':
    target = build()
    print(target)
    print('SHA256', hashlib.sha256(target.read_bytes()).hexdigest())
