"""Version 4 migrations, two independent queues and explicit user evidence."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from automation_store import AutomationStore
from comment_bank import CommentBank
from database import Database
from models import Video


@pytest.fixture
def store(tmp_path):
    db = Database(tmp_path / 'история с пробелом.sqlite3')
    store = AutomationStore(db)
    yield store
    db.close()


def links(count=1, start=0):
    return [(f'https://www.tiktok.com/@test/video/{n}', str(n)) for n in range(start, start + count)]


def bank(store, count=100):
    return CommentBank(store).import_text('\n'.join(f'Мой комментарий о RubUP номер {n}' for n in range(count)))


def complete(store, task_id):
    store.mark_filled(task_id)
    store.confirm_publication(task_id)
    return store.confirm_like(task_id)


def test_migration_backup_preserves_all_legacy_records(tmp_path):
    path = tmp_path / 'history.sqlite3'
    db = Database(path)
    db.add_video(Video('123', links(1, 123)[0][0]))
    old_task = db.ensure_task('old-account', '123')
    db.update_task(old_task['id'], comment='RubUP: старый комментарий')
    db.record_usage(old_task['id'], 'historical-model', 50, 10, .01, True, 1)
    old_schema = {}
    with db._connection() as conn:
        for table in ('videos', 'tasks', 'events', 'usage', 'telegram_outbox'):
            old_schema[table] = [tuple(row) for row in conn.execute(f'SELECT * FROM {table}')]
    store = AutomationStore(db)
    assert store.backup_path.is_file()
    with sqlite3.connect(store.backup_path) as backup:
        assert not backup.execute("SELECT 1 FROM sqlite_master WHERE name='v4_schema'").fetchone()
        for table, rows in old_schema.items():
            assert backup.execute(f'SELECT * FROM {table}').fetchall() == rows
    with db._connection() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 1
        assert conn.execute('SELECT version FROM v4_schema').fetchone()[0] == 4
        for table, rows in old_schema.items():
            assert [tuple(row) for row in conn.execute(f'SELECT * FROM {table}')] == rows
    backups = list(path.parent.glob('backups/*'))
    assert AutomationStore(db).backup_path is None
    assert list(path.parent.glob('backups/*')) == backups
    db.close()


def test_backup_failure_leaves_database_without_v4_additions(tmp_path, monkeypatch):
    db = Database(tmp_path / 'history.sqlite3')
    def fail(_):
        raise ValueError('backup failed')
    monkeypatch.setattr(AutomationStore, '_backup', fail)
    with pytest.raises(ValueError):
        AutomationStore(db)
    with db._connection() as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'v4_%'").fetchone()
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 1
    db.close()


def test_inmemory_migration_and_profile_ids_are_stable():
    db = Database(':memory:')
    store = AutomationStore(db)
    assert store.backup_path is None
    first = store.profiles()
    assert len(first) == 2 and {row['slot'] for row in first} == {1, 2}
    assert first[0]['id'] != first[1]['id']
    assert AutomationStore(db).profiles() == first
    db.close()


def test_profile_rename_retains_queue_identity_and_rejects_collision(store):
    before = store.profiles()
    store.add_videos(links())
    renamed = store.save_profile(1, '@мой аккаунт')
    assert renamed['id'] == before[0]['id']
    assert store.tasks(renamed['id'])[0]['profile_name'] == '@мой аккаунт'
    with pytest.raises(ValueError):
        store.save_profile(2, '@МОЙ   АККАУНТ')
    for slot, name in ((3, 'x'), (True, 'x'), (1, ''), (1, 'bad\nname')):
        with pytest.raises(ValueError):
            store.save_profile(slot, name)


def test_fifty_videos_create_100_independent_resumable_tasks(store):
    result = store.add_videos(links(50))
    assert result == {'added': 50, 'duplicates': 0, 'tasks': 100, 'legacy_published': 0}
    assert store.add_videos(links(50)) == {'added': 0, 'duplicates': 50, 'tasks': 0, 'legacy_published': 0}
    profile1, profile2 = store.profiles()
    assert len(store.tasks(profile1['id'])) == len(store.tasks(profile2['id'])) == 50
    bank(store)
    assigned = store.assign_comments()
    assert assigned == {'assigned': 100, 'missing': 0, 'available': 0}
    original = store.tasks()
    assert len({task['comment_id'] for task in original}) == 100
    assert len({task['comment'] for task in original}) == 100
    complete(store, original[0]['id'])
    restored_db = Database(store.db.path)
    restored = AutomationStore(restored_db)
    assert restored.profiles() == store.profiles()
    assert restored.tasks() == store.tasks()
    assert restored.assign_comments() == {'assigned': 0, 'missing': 0, 'available': 0}
    assert len(restored.tasks(profile1['id'], pending_only=True)) == 49
    assert len(restored.tasks(profile2['id'], pending_only=True)) == 50
    restored_db.close()


def test_invalid_input_is_checked_before_partial_queue_creation(store):
    with pytest.raises(ValueError):
        store.add_videos([*links(), ('https://evil.invalid/video/1', '1')])
    assert store.tasks() == []
    with pytest.raises(ValueError):
        store.add_videos([(links()[0][0], 'wrong-id')])
    assert store.tasks() == []


def test_earlier_confirmed_publication_skips_new_queues_without_using_bank(store):
    for number in (10, 11):
        store.db.add_video(Video(str(number), links(1, number)[0][0]))
    published = store.db.ensure_task('v3-account-one', '10')
    draft = store.db.ensure_task('v3-account-one', '11')
    store.db.update_task(published['id'], comment='RubUP: ранее опубликовано',
                         publication_status='PUBLISHED', processing_status='PUBLISHED')
    store.db.update_task(draft['id'], comment='RubUP: старый черновик', processing_status='DRAFT_READY')
    before = [store.db.get_task(row['id']) for row in (published, draft)]
    bank(store, 2)
    result = store.add_videos(links(1, 10))
    assert result == {'added': 0, 'duplicates': 1, 'tasks': 0, 'legacy_published': 1}
    assert store.tasks() == []
    assert store.assign_comments() == {'assigned': 0, 'missing': 0, 'available': 2}
    assert store.stats()['used_comments'] == 0
    # An earlier draft alone never blocks the user's new workflow.
    result = store.add_videos(links(1, 11))
    assert result == {'added': 1, 'duplicates': 0, 'tasks': 2, 'legacy_published': 0}
    assert store.assign_comments()['assigned'] == 2
    assert [store.db.get_task(row['id']) for row in (published, draft)] == before


def test_missing_bank_is_reported_and_retry_retains_allocated_comments(store):
    store.add_videos(links(2))
    bank(store, 3)
    assert store.assign_comments() == {'assigned': 3, 'missing': 1, 'available': 0}
    first = {row['id']: row['comment'] for row in store.tasks() if row['comment']}
    assert store.assign_comments()['missing'] == 1
    CommentBank(store).import_text('Ещё один комментарий о RubUP')
    assert store.assign_comments() == {'assigned': 1, 'missing': 0, 'available': 0}
    assert all(store.get_task(task_id)['comment'] == text for task_id, text in first.items())


def test_publication_and_own_like_require_separate_explicit_confirmation(store):
    store.add_videos(links())
    bank(store, 2)
    store.assign_comments()
    first, second = store.tasks()
    with pytest.raises(ValueError, match='Playwright'):
        store.confirm_publication(first['id'])
    with pytest.raises(ValueError, match='публикацию'):
        store.confirm_like(first['id'])
    store.mark_filled(first['id'])
    published = store.confirm_publication(first['id'])
    assert published['published'] is True and published['liked'] is False
    assert published['status'] == 'PUBLISHED'
    assert store.confirm_publication(first['id']) == published
    assert store.eligible_videos() == []
    done = store.confirm_like(first['id'])
    assert done['liked'] is True and done['status'] == 'DONE'
    assert store.confirm_like(first['id']) == done
    assert store.eligible_videos() == []
    complete(store, second['id'])
    assert store.eligible_videos() == [{'url': links()[0][0], 'video_id': '0'}]
    assert store.stats()['completed'] == 2
    assert store.stats()['telegram_pending'] == 1


def test_failed_fill_revokes_only_unconfirmed_field_evidence(store):
    store.add_videos(links())
    task = store.tasks()[0]
    with pytest.raises(ValueError):
        store.mark_filled(task['id'])
    bank(store, 2)
    store.assign_comments()
    task = store.mark_filled(task['id'])
    failed = store.mark_error(task['id'], 'Поле не найдено')
    assert failed['status'] == 'FAILED' and failed['filled_at'] is None
    assert failed['comment_id'] == task['comment_id']
    with pytest.raises(ValueError):
        store.confirm_publication(task['id'])
    done = complete(store, task['id'])
    after = store.mark_error(task['id'], 'Не удалось открыть браузер')
    assert after['status'] == 'DONE' and after['published'] and after['liked']
    assert after['comment'] == done['comment']


def test_settings_survive_reopen_without_storing_credentials(store):
    store.set_setting('state', 'PAUSED')
    store.set_setting('telegram_enabled', True)
    assert AutomationStore(store.db).get_setting('state') == 'PAUSED'
    assert store.get_setting('missing', {'default': 1}) == {'default': 1}
    assert store.get_setting('telegram_enabled') is True
    for key, value in [('telegram_token', 'sensitive'), ('wrong key', True), ('state', float('nan'))]:
        with pytest.raises(ValueError):
            store.set_setting(key, value)


def test_telegram_statistics_respect_historical_sent_and_uncertain_reservations(store):
    store.add_videos(links(2))
    bank(store, 4)
    store.assign_comments()
    for task in store.tasks():
        complete(store, task['id'])
    assert store.stats()['telegram_pending'] == 2
    assert store.db.reserve_telegram(links()[0][0])
    assert store.stats()['telegram_pending'] == 1
    assert store.stats()['telegram_unknown'] == 1
    store.db.finish_telegram(links()[0][0], 'SENT')
    assert store.stats()['telegram_sent'] == 1
    assert store.stats()['telegram_unknown'] == 0


def test_assignment_is_atomic_across_two_store_instances(store):
    store.add_videos(links(50))
    bank(store)
    db2 = Database(store.db.path)
    other = AutomationStore(db2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda item: item.assign_comments(), (store, other)))
    assert sum(result['assigned'] for result in outcomes) == 100
    tasks = store.tasks()
    assert len({task['comment_id'] for task in tasks}) == 100
    assert all(task['status'] == 'READY' for task in tasks)
    db2.close()
