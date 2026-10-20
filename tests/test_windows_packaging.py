"""Portable distribution checks; Windows execution itself needs a Windows host."""
from pathlib import Path
import zipfile

import pytest

import tools.package_project as packager


def test_archive_from_cyrillic_path_includes_launchers_and_safe_assets(tmp_path, monkeypatch):
    root = tmp_path / 'Мои программы' / 'RubUP Automation'
    (root / 'tools').mkdir(parents=True)
    (root / 'docs').mkdir()
    (root / 'data').mkdir()
    (root / 'images').mkdir()
    (root / 'logs').mkdir()
    (root / 'gui').mkdir()
    for name in ('main.py', 'start.bat', 'install.bat', 'setup_windows.ps1',
                 'build_windows.ps1', 'README.md', '.env.example'):
        (root / name).write_text('тестовый исходник', encoding='utf-8')
    (root / 'docs' / 'v2-home.png').write_bytes(b'known documentation screenshot')
    (root / 'data' / 'credentials.dpapi').write_bytes(b'private encrypted credential data')
    (root / '.env').write_text('OPENAI_API_KEY=private-local-value', encoding='utf-8')
    (root / 'images' / 'original.png').write_bytes(b'private user picture')
    (root / 'logs' / 'rubup.log').write_text('private local history', encoding='utf-8')
    (root / 'RubUP-TikTok-Automation.zip').write_bytes(b'old nested release')
    monkeypatch.setattr(packager, '__file__', str(root / 'tools' / 'package_project.py'))
    target = packager.build(tmp_path / 'Готовая сборка.zip')
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        assert f'{root.name}/install.bat' in names
        assert f'{root.name}/setup_windows.ps1' in names
        assert f'{root.name}/docs/v2-home.png' in names
        assert not any('credentials.dpapi' in n or n.endswith('/.env') or '/images/original' in n
                       or n.endswith('rubup.log') or n.endswith('Automation.zip') for n in names)
        assert archive.testzip() is None


def test_failed_packaging_preserves_previous_zip_and_external_files(tmp_path, monkeypatch):
    root = tmp_path / 'project'
    (root / 'tools').mkdir(parents=True)
    private = tmp_path / 'private.py'
    private.write_text('private key material', encoding='utf-8')
    try:
        (root / 'main.py').symlink_to(private)
    except OSError:
        pytest.skip('Creating a symlink is unavailable on this test host')
    monkeypatch.setattr(packager, '__file__', str(root / 'tools' / 'package_project.py'))
    target = tmp_path / 'existing.zip'
    target.write_bytes(b'previous verified release')
    with pytest.raises(ValueError, match='regular file'):
        packager.build(target)
    assert target.read_bytes() == b'previous verified release'
    assert private.read_text(encoding='utf-8') == 'private key material'
    assert not list(tmp_path.glob('.rubup-release-*'))


def test_launchers_use_windows_line_endings_without_utf8_bom():
    root = Path(__file__).resolve().parents[1]
    for name in ('start.bat', 'install.bat'):
        raw = (root / name).read_bytes()
        assert not raw.startswith(b'\xef\xbb\xbf')
        assert raw.startswith(b'@echo off\r\nchcp 65001 >nul\r\n')
        assert b'\n' not in raw.replace(b'\r\n', b'')
        raw.decode('utf-8')
    assert 'pythonw.exe' in (root / 'start.bat').read_text(encoding='utf-8')


def test_powershell_scripts_preserve_local_user_state():
    root = Path(__file__).resolve().parents[1]
    setup = (root / 'setup_windows.ps1').read_text(encoding='utf-8-sig')
    build = (root / 'build_windows.ps1').read_text(encoding='utf-8-sig')
    assert 'Set-Location -LiteralPath $PSScriptRoot' in setup
    assert 'Set-Location -LiteralPath $PSScriptRoot' in build
    assert 'Remove-Item' not in setup and 'Remove-Item' not in build
    assert 'Copy-Item' not in setup  # A new template must not replace existing config or credentials.
    assert '--windowed' in build and 'RubUP-Automation' in build
    assert '-LiteralPath (Join-Path $PSScriptRoot ".env")' not in build
