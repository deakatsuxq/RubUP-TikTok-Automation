"""Windows 4.0 distribution includes browser setup and preserves private state."""
from pathlib import Path
import zipfile

import pytest

import tools.package_project as packager


def source_tree(tmp_path):
    root = tmp_path / 'Программа с пробелами'
    (root / 'tools').mkdir(parents=True)
    (root / 'tests' / 'fixtures' / 'browser').mkdir(parents=True)
    (root / 'legacy_v3' / 'gui').mkdir(parents=True)
    (root / 'VERSION').write_text('4.0\n', encoding='utf-8')
    (root / 'main.py').write_text('current source', encoding='utf-8')
    (root / 'config.yaml').write_text('account_id: private-old-account\nmodel: historical\n', encoding='utf-8')
    (root / 'config.v4.example.yaml').write_text('db_path: data/history.sqlite3\n', encoding='utf-8')
    (root / 'tests' / 'fixtures' / 'browser' / 'comments.html').write_text('<textarea></textarea>', encoding='utf-8')
    (root / 'legacy_v3' / 'gui' / 'old_window.py').write_text('historical source', encoding='utf-8')
    (root / 'legacy_v3' / 'README.md').write_text('Regression archive only', encoding='utf-8')
    return root


def test_v4_zip_uses_version_and_clean_config_without_touching_user_files(tmp_path, monkeypatch):
    root = source_tree(tmp_path)
    original = (root / 'config.yaml').read_bytes()
    old_zip = tmp_path / 'RubUP-Automation-3.0-Windows.zip'
    old_zip.write_bytes(b'previous release')
    monkeypatch.setattr(packager, '__file__', str(root / 'tools' / 'package_project.py'))
    output = packager.build()
    assert output.name == 'RubUP-Automation-4.0-Windows.zip'
    assert old_zip.read_bytes() == b'previous release'
    assert (root / 'config.yaml').read_bytes() == original
    with zipfile.ZipFile(output) as archive:
        assert archive.read(f'{root.name}/config.yaml') == (root / 'config.v4.example.yaml').read_bytes()
        assert b'private-old-account' not in archive.read(f'{root.name}/config.yaml')
        assert f'{root.name}/tests/fixtures/browser/comments.html' in archive.namelist()
        assert f'{root.name}/legacy_v3/gui/old_window.py' in archive.namelist()
        assert f'{root.name}/legacy_v3/README.md' in archive.namelist()
        assert '  config.yaml\n' in archive.read(f'{root.name}/SHA256SUMS.txt').decode()


def test_profile_cookies_and_fixture_runtime_files_are_not_packaged(tmp_path, monkeypatch):
    root = source_tree(tmp_path)
    profile = root / 'data' / 'playwright_profiles' / 'account-1'
    profile.mkdir(parents=True)
    (profile / 'Cookies').write_bytes(b'private session')
    (root / 'tests' / 'fixtures' / 'browser' / 'runtime.sqlite3').write_bytes(b'private DB')
    (root / 'legacy_v3' / 'private.env').write_bytes(b'private key')
    monkeypatch.setattr(packager, '__file__', str(root / 'tools' / 'package_project.py'))
    with zipfile.ZipFile(packager.build()) as archive:
        names = archive.namelist()
        assert not any('Cookies' in name or 'playwright_profiles' in name or name.endswith(('.sqlite3', '.env'))
                       for name in names)


def test_unsafe_template_symlink_preserves_previous_archive(tmp_path, monkeypatch):
    root = source_tree(tmp_path)
    template = root / 'config.v4.example.yaml'
    template.unlink()
    outside = tmp_path / 'private-config.yaml'
    outside.write_text('secret: value', encoding='utf-8')
    try:
        template.symlink_to(outside)
    except OSError:
        pytest.skip('Symlinks unavailable on this host')
    output = tmp_path / 'last-good.zip'
    output.write_bytes(b'previous good archive')
    monkeypatch.setattr(packager, '__file__', str(root / 'tools' / 'package_project.py'))
    with pytest.raises(ValueError, match='regular file'):
        packager.build(output)
    assert output.read_bytes() == b'previous good archive'


def test_windows_installs_pinned_browser_and_exe_contains_its_driver():
    root = Path(__file__).resolve().parents[1]
    setup = (root / 'setup_windows.ps1').read_text(encoding='utf-8-sig')
    build = (root / 'build_windows.ps1').read_text(encoding='utf-8-sig')
    helper = (root / 'install_chromium.ps1').read_text(encoding='utf-8-sig')
    assert '& $python -m playwright install chromium' in setup
    assert '--collect-all playwright' in build
    assert 'config.v4.example.yaml' in build
    assert 'install_chromium.bat' in build and 'install_chromium.ps1' in build
    assert '_internal\\playwright\\driver' in helper
    assert '& $node $cli install chromium' in helper
    assert 'Remove-Item' not in setup + build + helper
    assert 'OPENAI_API_KEY' not in (root / '.env.example').read_text(encoding='utf-8')
    assert 'playwright==1.64.1' in (root / 'requirements.txt').read_text(encoding='utf-8')


def test_chromium_bat_uses_windows_utf8_crlf_without_bom():
    raw = (Path(__file__).resolve().parents[1] / 'install_chromium.bat').read_bytes()
    assert raw.startswith(b'@echo off\r\nchcp 65001 >nul\r\n')
    assert not raw.startswith(b'\xef\xbb\xbf')
    assert b'\n' not in raw.replace(b'\r\n', b'')
    raw.decode('utf-8')
