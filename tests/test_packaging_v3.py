"""Windows source release keeps prior versions and private user data out of ZIP."""
from pathlib import Path
import zipfile

from gui.help_page import HELP
import tools.package_project as packager


def project(tmp_path):
    root = tmp_path / 'Приложение с пробелами'
    for folder in ('tools', 'docs', 'gui', 'tests', 'data', 'images', 'logs'):
        (root / folder).mkdir(parents=True, exist_ok=True)
    for name in ('main.py', 'README.md', 'requirements.txt', 'start.bat', 'install.bat',
                 'build_windows.ps1', '.env.example'):
        (root / name).write_text('source', encoding='utf-8')
    return root


def test_versioned_default_zip_preserves_v2_and_excludes_private_backups(tmp_path, monkeypatch):
    root = project(tmp_path)
    old = tmp_path / 'RubUP-TikTok-Automation.zip'
    old.write_bytes(b'previous-v2-archive')
    (root / 'data' / 'backups').mkdir()
    (root / 'data' / 'backups' / 'before-v3.sqlite3').write_bytes(b'private history')
    (root / 'data' / 'credentials.dpapi').write_bytes(b'private encrypted keys')
    (root / '.env').write_text('OPENAI_API_KEY=private', encoding='utf-8')
    (root / 'docs' / 'v3-home.png').write_bytes(b'public screenshot')
    (root / 'RubUP-TikTok-Automation.zip').write_bytes(b'nested prior version')
    monkeypatch.setattr(packager, '__file__', str(root / 'tools' / 'package_project.py'))
    archive_path = packager.build()
    assert archive_path.name == 'RubUP-Automation-3.0-Windows.zip'
    assert old.read_bytes() == b'previous-v2-archive'
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        assert f'{root.name}/docs/v3-home.png' in names
        assert not any('/backups/' in n or 'credentials.dpapi' in n or n.endswith('/.env')
                       or n.endswith('.sqlite3') or n.endswith('.zip') for n in names)
        assert archive.testzip() is None


def test_windows_build_targets_new_release_directory_instead_of_user_installation():
    script = (Path(__file__).resolve().parents[1] / 'build_windows.ps1').read_text(encoding='utf-8-sig')
    assert '--distpath $releaseRoot' in script
    assert 'Get-Date -Format' in script
    assert 'Test-Path -LiteralPath $releaseRoot' in script
    assert '--windowed' in script
    assert 'Remove-Item' not in script
    assert 'credentials.dpapi' not in script


def test_embedded_help_covers_batch_profiles_manual_actions_and_safe_delivery():
    text = '\n'.join(html for _, html in HELP)
    assert len(HELP) == 6  # Preserve navigation of previous version.
    for phrase in ('50', '100', 'аккаунт', 'НАЧАТЬ ПОДГОТОВКУ', 'Я поставил лайк',
                   'Ctrl+1', 'Ctrl+2', 'Ctrl+→', 'обоими', 'getUpdates', 'ChatGPT',
                   'неподтверждён', 'backup-v2', 'rubup-v3'):
        assert phrase.casefold() in text.casefold()
