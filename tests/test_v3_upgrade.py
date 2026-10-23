"""An upgrade preserves old identity/history and creates a recoverable backup."""
from dataclasses import replace
import sqlite3

import pytest

from application import AppService
from batch_storage import BatchStore
from config import Config, load_config, save_config
from database import Database
from models import Video
from report_tracker import ReportTracker


@pytest.fixture
def old_installation(tmp_path):
    root = tmp_path / 'Старая версия с историей'
    cfg = root / 'config.yaml'
    save_config(Config(db_path=str(root / 'data/history.sqlite3'), account_id='my-old-account'), cfg)
    app = AppService(cfg)
    app.complete_onboarding()
    app.db.add_video(Video('123', 'https://www.tiktok.com/@example/video/123',
                           description='#cs2 обзор', hashtags=['cs2'], published_at='2026-06-01'))
    task = app.db.ensure_task(app.config.account_id, '123')
    app.save_comment(task['id'], 'реклама: какие скины в RubUP?')
    app.db.record_usage(task['id'], 'old-model', 100, 10, .001, True, 1)
    yield app, task['id']
    app.close()


def test_legacy_startup_does_not_migrate_database_or_reset_user_files(old_installation):
    app, task_id = old_installation
    config_before = app.config_path.read_bytes()
    state_before = app.user_state.path.read_bytes()
    with sqlite3.connect(app.db.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='batch_schema'").fetchone()[0] == 0
    original = app.db.get_task(task_id)
    store = BatchStore(app.db)
    assert store.backup_path.is_file()
    with sqlite3.connect(store.backup_path) as backup:
        assert backup.execute('SELECT comment FROM tasks WHERE id=?', (task_id,)).fetchone()[0] == original['comment']
        assert backup.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='batch_schema'").fetchone()[0] == 0
    profiles = store.ensure_profiles(app.config.account_id)
    assert profiles[0]['profile_id'] == 'my-old-account'
    assert profiles[1]['profile_id'] != profiles[0]['profile_id']
    assert app.db.get_task(task_id) == original
    assert app.db.usage_summary()['requests'] == 1
    assert app.config_path.read_bytes() == config_before
    assert app.user_state.path.read_bytes() == state_before
    assert app.onboarding_complete()
    reopened = BatchStore(app.db)
    assert reopened.backup_path is None
    assert reopened.ensure_profiles(app.config.account_id) == profiles
    with sqlite3.connect(app.db.path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 1


def test_backup_failure_leaves_v2_database_usable(old_installation, monkeypatch):
    app, task_id = old_installation
    def failure(self):
        raise ValueError('Не удалось создать резервную копию')
    monkeypatch.setattr(BatchStore, '_backup', failure)
    with pytest.raises(ValueError, match='резервную'):
        BatchStore(app.db)
    assert app.db.get_task(task_id)['comment']
    with sqlite3.connect(app.db.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='batch_schema'").fetchone()[0] == 0
    assert app.prepare_pending()['prepared'] == 0


def test_pending_like_remains_distinct_from_publication(old_installation):
    app, task_id = old_installation
    row = app.db.get_task(task_id)
    app.db.update_task(task_id, checklist={**row['checklist'], 'require_like': True})
    confirmed = {'published': True, 'comment_present': True, 'comment_not_deleted': True}
    with pytest.raises(ValueError, match='лайк'):
        app.report_tracker.confirm_publication(task_id, confirmed)
    row = app.report_tracker.confirm_publication(task_id, confirmed, allow_pending_like=True)
    assert row['publication_status'] == 'PUBLISHED'
    assert not row['checklist'].get('own_comment_liked')
    assert row['telegram_status'] == 'NOT_SENT'
    assert row['report_status'] == 'NONE'
    assert row['reward_status'] == 'UNCONFIRMED'


def test_legacy_yaml_gets_safe_batch_defaults_without_rewriting(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text('model: gpt-4.1-mini\naccount_id: old\n', encoding='utf-8')
    raw = path.read_bytes()
    config = load_config(path)
    assert config.batch_budget_usd is None
    assert config.batch_telegram_mode == 'both'
    assert path.read_bytes() == raw


@pytest.mark.parametrize('limit', [-1, True, float('nan'), float('inf')])
def test_invalid_expense_limits_are_rejected(limit):
    with pytest.raises(ValueError):
        Config(batch_budget_usd=limit).validate()
