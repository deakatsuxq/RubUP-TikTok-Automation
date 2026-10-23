"""Additive migration and durable independent work records preserve version 2."""
from pathlib import Path
import sqlite3

import pytest

from batch_storage import BatchStore
from database import Database
from models import Video


@pytest.fixture
def storage(tmp_path):
    db = Database(tmp_path / 'history.sqlite3')
    store = BatchStore(db)
    store.ensure_profiles('legacy-account')
    yield db, store
    db.close()


def add_video(db, number='123'):
    db.add_video(Video(number, f'https://www.tiktok.com/@fixture/video/{number}',
                       description='#cs2 test', published_at='2026-06-01'))


def make_batch(store):
    return store.create_batch([profile['profile_id'] for profile in store.list_profiles()])


def test_migration_backup_has_legacy_rows_before_additions(tmp_path):
    path = tmp_path / 'history.sqlite3'
    db = Database(path)
    add_video(db)
    task = db.ensure_task('legacy-account', '123')
    db.update_task(task['id'], comment='реклама: что там в RubUP?')
    db.record_usage(task['id'], 'gpt-4.1-mini', 12, 4, .002, True, 1)
    store = BatchStore(db)
    assert store.backup_path.is_file()
    with sqlite3.connect(store.backup_path) as backup:
        assert backup.execute('SELECT comment FROM tasks').fetchone()[0] == 'реклама: что там в RubUP?'
        assert backup.execute('SELECT COUNT(*) FROM usage').fetchone()[0] == 1
        assert not backup.execute("SELECT 1 FROM sqlite_master WHERE name='batch_schema'").fetchone()
    with sqlite3.connect(path) as current:
        assert current.execute('PRAGMA user_version').fetchone()[0] == 1
        assert current.execute('SELECT version FROM batch_schema').fetchone()[0] == 3
    assert db.get_task(task['id'])['comment'] == 'реклама: что там в RubUP?'
    previous_copies = list(path.parent.glob('backups/*'))
    reopened = BatchStore(db)
    assert reopened.backup_path is None
    assert list(path.parent.glob('backups/*')) == previous_copies
    db.close()


def test_backup_failure_leaves_original_schema_unchanged(tmp_path, monkeypatch):
    db = Database(tmp_path / 'history.sqlite3')
    add_video(db)
    def fail(_):
        raise ValueError('backup unavailable')
    monkeypatch.setattr(BatchStore, '_backup', fail)
    with pytest.raises(ValueError):
        BatchStore(db)
    with db._connection() as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='batch_schema'").fetchone()
        assert conn.execute('SELECT COUNT(*) FROM videos').fetchone()[0] == 1
    db.close()


def test_in_memory_schema_needs_no_file_backup():
    db = Database(':memory:')
    store = BatchStore(db)
    assert store.backup_path is None
    assert len(store.ensure_profiles('original')) == 2
    db.close()


def test_profiles_stay_stable_without_renaming_legacy_history(storage):
    db, store = storage
    add_video(db)
    task = db.ensure_task('legacy-account', '123')
    first = store.list_profiles()
    again = store.ensure_profiles('some-other-later-setting')
    assert first == again
    assert first[0]['profile_id'] == 'legacy-account'
    assert first[1]['profile_id'] != first[0]['profile_id']
    assert db.get_task(task['id'])['account_id'] == 'legacy-account'
    with pytest.raises(ValueError):
        store.save_profile(1, profile_id='rename')


def test_two_accounts_have_independent_tasks_same_video(storage):
    db, store = storage
    add_video(db)
    a, b = [db.ensure_task(profile['profile_id'], '123') for profile in store.list_profiles()]
    db.update_task(a['id'], comment='реклама: какие скины в RubUP?')
    assert db.get_task(b['id'])['comment'] == ''
    assert db.ensure_task(a['account_id'], '123')['id'] == a['id']
    assert a['id'] != b['id']


def test_import_fifty_links_deduplicates_and_resumes(storage):
    db, store = storage
    batch = make_batch(store)
    urls = [f'https://www.tiktok.com/@fixture/video/{number}' for number in range(50)]
    imported = store.add_inputs(batch['id'], urls)
    repeated = store.add_inputs(batch['id'], [urls[1], urls[4]])
    assert imported['accepted'] == len(imported['items']) == 50
    assert repeated['duplicates'] == 2 and repeated['accepted'] == 0
    assert [item['position'] for item in store.list_items(batch['id'])] == list(range(1, 51))
    item = imported['items'][0]
    add_video(db, '0')
    store.update_item(item['id'], video_id='0', state='READY', metadata_attempted=True)
    store.set_batch_state(batch['id'], 'paused')
    reopened_db = Database(db.path)
    restored = BatchStore(reopened_db)
    assert restored.get_batch(batch['id'])['state'] == 'PAUSED'
    restored_item = restored.get_item(item['id'])
    assert restored_item['metadata_attempted'] is True
    assert restored_item['video_id'] == '0' and restored_item['state'] == 'READY'
    assert len(restored.list_items(batch['id'])) == 50
    reopened_db.close()


def test_find_video_alias_excludes_duplicates_and_current_item(storage):
    db, store = storage
    add_video(db)
    batch = make_batch(store)
    rows = store.add_inputs(batch['id'], ['https://vt.tiktok.com/a/', 'https://vt.tiktok.com/b/'])['items']
    store.update_item(rows[0]['id'], video_id='123', state='RESOLVED')
    assert store.find_item(batch['id'], '123', exclude_id=rows[1]['id'])['id'] == rows[0]['id']
    assert store.find_item(batch['id'], '123', exclude_id=rows[0]['id']) is None
    store.update_item(rows[0]['id'], state='DUPLICATE')
    assert store.find_item(batch['id'], '123') is None


def test_telegram_queue_uses_global_outbox_and_blocks_uncertain_retry(storage):
    db, store = storage
    add_video(db)
    batch = make_batch(store)
    url = db.get_video('123')['url']
    assert store.enqueue_telegram(url, '123', batch['id'])
    assert not store.enqueue_telegram(url, '123', batch['id'])
    store.update_telegram(url, 'IN_FLIGHT')
    store.update_telegram(url, 'UNKNOWN', 'response lost')
    with pytest.raises(ValueError):
        store.update_telegram(url, 'PENDING')
    assert store.telegram_queue(batch['id'])[0]['status'] == 'UNKNOWN'
    store.update_telegram(url, 'SENT')
    with pytest.raises(ValueError):
        store.update_telegram(url, 'FAILED')


@pytest.mark.parametrize('outbox_state,expected', [('SENT', 'SENT'), ('UNKNOWN', 'UNKNOWN'), ('RESERVED', 'UNKNOWN')])
def test_already_sent_v2_link_is_not_enqueued_again(storage, outbox_state, expected):
    db, store = storage
    add_video(db)
    batch = make_batch(store)
    url = db.get_video('123')['url']
    assert db.reserve_telegram(url)
    db.finish_telegram(url, outbox_state)
    assert not store.enqueue_telegram(url, '123', batch['id'])
    assert store.telegram_queue()[0]['status'] == expected


def test_invalid_writes_are_rejected_before_any_partial_import(storage):
    db, store = storage
    batch = make_batch(store)
    with pytest.raises(ValueError):
        store.add_inputs(batch['id'], ['https://vt.tiktok.com/a/', 'bad\nline'])
    assert store.list_items(batch['id']) == []
    with pytest.raises(ValueError):
        store.create_batch([store.list_profiles()[0]['profile_id']] * 2)
    with pytest.raises(ValueError):
        store.create_batch([p['profile_id'] for p in store.list_profiles()], limit_usd=float('nan'))


def test_duplicate_username_cannot_mislabel_two_actual_accounts(storage):
    _, store = storage
    store.save_profile(1, username='@alice')
    with pytest.raises(ValueError, match='разных'):
        store.save_profile(2, username='ALICE')
