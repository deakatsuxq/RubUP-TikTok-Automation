"""Build a source-only archive from an explicit allowlist; never package secrets."""
from pathlib import Path
import hashlib
import os
import re
import tempfile
import zipfile


def build(destination=None):
    root = Path(__file__).resolve().parents[1]
    # VERSION is explicit release metadata. Repackaging a legacy 3.0 source tree
    # without this file retains its original archive name.
    version_path = root / 'VERSION'
    version = version_path.read_text(encoding='utf-8').strip() if version_path.exists() else '3.0'
    if not re.fullmatch(r'[0-9]+\.[0-9]+(?:\.[0-9]+)?', version):
        raise ValueError('VERSION must contain a numeric release version')
    target = Path(destination) if destination else root.parent / f'RubUP-Automation-{version}-Windows.zip'
    target.parent.mkdir(parents=True, exist_ok=True)
    names = ['README.md', 'VERSION', 'requirements.txt', 'config.yaml', 'config.v4.example.yaml',
             '.env.example', '.gitignore', '.gitattributes',
             'pytest.ini', 'start.bat', 'install.bat', 'setup_windows.ps1', 'build_windows.ps1',
             'install_chromium.bat', 'install_chromium.ps1',
             'images/.gitkeep', 'data/.gitkeep', 'logs/.gitkeep']
    files = [root / name for name in names if (root / name).is_file()]
    files += list(root.glob('*.py'))
    for folder in ('gui', 'tests', 'tools'):
        files += list((root / folder).glob('*.py'))
    # Historical source is retained to run the unchanged 2.0/3.0 regression
    # tests. Production main and the EXE do not import this archive.
    files += [path for path in (root / 'legacy_v3').rglob('*.py')
              if '__pycache__' not in path.parts]
    legacy_readme = root / 'legacy_v3' / 'README.md'
    if legacy_readme.is_file():
        files.append(legacy_readme)
    # Browser fixtures are reviewed static test pages, never browser profiles.
    fixture_root = root / 'tests' / 'fixtures'
    files += [path for path in fixture_root.rglob('*')
              if path.is_file() and path.suffix.lower() in {'.html', '.js', '.css', '.json'}]
    files += list((root / 'docs').glob('*.md'))
    files += list((root / 'docs').glob('*.png'))
    config_template = root / 'config.v4.example.yaml'
    if config_template.is_file():
        # Source checkouts can contain existing user options. A release gets a
        # clean 4.0 template; installation never copies it over an existing file.
        files = [path for path in files if path != root / 'config.yaml']
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
            if config_template.is_file():
                if config_template.is_symlink() or not config_template.resolve().is_relative_to(root.resolve()):
                    raise ValueError('Archive source must be a regular file inside the project')
                raw = config_template.read_bytes()
                hashes.append(f'{hashlib.sha256(raw).hexdigest()}  config.yaml')
                archive.writestr(f'{root.name}/config.yaml', raw)
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
