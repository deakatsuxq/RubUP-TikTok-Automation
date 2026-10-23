"""Browser-profile launch stays explicit and never claims authentication."""
from pathlib import Path
import subprocess

import pytest

from batch_storage import BatchStore
from database import Database
from models import Video
from profiles import ProfileManager


@pytest.fixture
def manager(tmp_path):
    db = Database(tmp_path / 'history.sqlite3')
    store = BatchStore(db)
    store.ensure_profiles('first')
    yield ProfileManager(store, db), db
    db.close()


def test_counts_and_unknown_auth_preserve_independent_accounts(manager):
    profiles, db = manager
    db.add_video(Video('123', 'https://www.tiktok.com/@alice/video/123'))
    task = db.ensure_task('first', '123')
    db.update_task(task['id'], comment='реклама: что там в RubUP?')
    rows = profiles.list()
    assert rows[0]['drafts'] == 1 and rows[1]['drafts'] == 0
    assert all(row['published'] == 0 for row in rows)
    assert all(row['auth_status'] == 'Вход не подтверждён программой' for row in rows)


def test_default_open_uses_validated_official_url_only(manager, monkeypatch):
    profiles, _ = manager
    calls = []
    monkeypatch.setattr('profiles.webbrowser.open', lambda url, new: calls.append((url, new)) or True)
    profiles.save(1, username='@alice')
    profiles.open_profile('first')
    profiles.open_video('first', 'https://tiktok.com/@alice/video/123?tracking=1')
    assert calls == [('https://www.tiktok.com/@alice', 2), ('https://www.tiktok.com/@alice/video/123', 2)]
    with pytest.raises(ValueError):
        profiles.open_video('first', 'https://localhost/private')
    assert len(calls) == 2


def test_configured_profile_uses_argument_list_and_no_shell(manager, tmp_path, monkeypatch):
    profiles, _ = manager
    browser = tmp_path / 'Папка с пробелом' / 'chrome.exe'
    browser.parent.mkdir()
    browser.write_text('test file, never executed')
    browser.chmod(0o700)
    profiles.save(1, username='alice', browser_kind='chrome', browser_path=str(browser), browser_profile='Profile 1')
    calls = []
    monkeypatch.setattr(subprocess, 'Popen', lambda command, shell: calls.append((command, shell)))
    assert profiles.open_video('first', 'https://www.tiktok.com/@alice/video/123')
    assert calls == [([str(browser.resolve()), '--profile-directory=Profile 1', 'https://www.tiktok.com/@alice/video/123'], False)]
    assert profiles.list()[0]['auth_status'] == 'Вход не подтверждён программой'


@pytest.mark.parametrize('username', ['alice/example', 'alice;command', 'x' * 25, 'alice@evil'])
def test_username_cannot_inject_url(manager, username):
    profiles, _ = manager
    with pytest.raises(ValueError):
        profiles.save(1, username=username)


@pytest.mark.parametrize('label', ['../Profile 1', '--remote-debugging-port=9222', 'Profile\n2', 'C:\\Cookies'])
def test_browser_profile_cannot_inject_path_or_command(manager, label):
    profiles, _ = manager
    with pytest.raises(ValueError):
        profiles.save(1, browser_profile=label)


def test_arbitrary_program_is_not_accepted_as_browser(manager, tmp_path):
    profiles, _ = manager
    program = tmp_path / 'program.exe'
    program.write_text('do not run'); program.chmod(0o700)
    with pytest.raises(ValueError):
        profiles.save(1, browser_kind='chrome', browser_path=str(program))


def test_missing_browser_does_not_invent_success(manager, monkeypatch):
    profiles, _ = manager
    profiles.save(1, browser_kind='edge', browser_profile='Profile 2')
    monkeypatch.setattr('profiles.shutil.which', lambda name: None)
    with pytest.raises(ValueError, match='не найден'):
        profiles.open_profile('first')


def test_two_accounts_cannot_share_explicit_same_browser_profile(manager, tmp_path):
    profiles, _ = manager
    browser = tmp_path / 'chrome.exe'
    browser.write_text('fixture, never executed'); browser.chmod(0o700)
    profiles.save(1, username='alice', browser_kind='chrome', browser_path=str(browser), browser_profile='Profile 1')
    with pytest.raises(ValueError, match='разные профили'):
        profiles.save(2, username='bob', browser_kind='chrome', browser_path=str(browser), browser_profile='PROFILE 1')
    saved = profiles.save(2, username='bob', browser_kind='chrome', browser_path=str(browser), browser_profile='Profile 2')
    assert saved['browser_profile'] == 'Profile 2'
    assert saved['auth_status'] == 'Вход не подтверждён программой'


def test_default_browser_allows_manual_switch_but_explains_unconfirmed_login(manager):
    profiles, _ = manager
    profiles.save(1, username='alice')
    profiles.save(2, username='bob')
    assert all(profile['browser_kind'] == 'default' for profile in profiles.list())
    assert all('проверьте' in profile['browser_warning'] for profile in profiles.list())
