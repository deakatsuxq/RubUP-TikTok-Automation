"""The simple single-link flow preserves evidence, money and old task state."""
import sqlite3
import threading
from pathlib import Path

import httpx
import pytest

from application import AppService
from config import Config, save_config
from database import Database
from models import Video
from tiktok_search import DiscoveryError
from validators import ValidationError


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(httpx.Client, 'request', lambda *a, **k: pytest.fail('unexpected network'))
    path = tmp_path / 'Папка с пробелами' / 'config.yaml'
    save_config(Config(db_path=str(path.parent / 'data/history.sqlite3')), path)
    app = AppService(path)
    calls = []
    def generate(task_id, video):
        calls.append((task_id, video))
        return f"реклама: что обсудим в RubUP {video['video_id']}?"
    app.generator.generate = generate
    app.generation_calls = calls
    yield app
    app.close()


def candidate(app, video_id='123', date=None, description='#cs2 обзор скинов'):
    app.db.add_video(Video(video_id, f'https://www.tiktok.com/@fixture/video/{video_id}',
                           description=description, published_at=date))
    return app.db.ensure_task(app.config.account_id, video_id)


def checks():
    return {'published': True, 'comment_present': True, 'comment_not_deleted': True}


def test_single_click_allows_preliminary_but_blocks_confirmation_and_send(service):
    task = candidate(service)
    progress = []
    result = service.process_url(task['url'], progress=progress.append)
    assert result['review']['missing'] == ['date']
    assert result['review']['eligible'] is False
    assert result['processing_status'] == 'DRAFT_READY'
    assert result['checklist']['preliminary'] is True
    assert 'RubUP' in result['comment']
    assert len(service.generation_calls) == 1
    assert progress == ['Проверяем ссылку…', 'Получаем доступные сведения…',
                        'Готовим комментарий…', 'Проверяем результат…']
    with pytest.raises(ValidationError):
        service.confirm_publication(task['id'], checks())
    with pytest.raises(ValidationError):
        service.send_task_link(task['id'])
    with pytest.raises(ValueError):
        service.report_tracker.confirm_publication(task['id'], checks())
    assert service.stats()['published'] == service.stats()['telegram_sent'] == 0


def test_fill_only_missing_date_promotes_same_cached_comment_without_charge(service):
    task = candidate(service)
    first = service.process_url(task['url'])
    updated = service.update_metadata(task['id'], '2026-06-01', first['description'])
    assert updated['comment'] == first['comment']
    assert updated['checklist']['preliminary'] is False
    assert updated['processing_status'] == 'PENDING_APPROVAL'
    confirmed = service.confirm_publication(task['id'], checks())
    assert confirmed['publication_status'] == 'PUBLISHED'
    assert confirmed['report_status'] == 'NONE'
    assert len(service.generation_calls) == 1


def test_cached_flow_skips_oembed_and_paid_generation(service, monkeypatch):
    task = candidate(service, date='2026-06-01')
    first = service.process_url(task['url'])
    monkeypatch.setattr(service.search, 'enrich', lambda *a: pytest.fail('cached enrich'))
    result = service.process_url(task['url'])
    assert result['cached'] is True
    assert result['comment'] == first['comment']
    assert len(service.generation_calls) == 1


def test_unavailable_metadata_is_not_invented_and_neutral_draft_is_allowed(service, monkeypatch):
    def unavailable(video_id):
        raise DiscoveryError('Недоступно')
    monkeypatch.setattr(service.search, 'enrich', unavailable)
    result = service.process_url('https://www.tiktok.com/@fixture/video/100')
    assert set(result['review']['missing']) == {'date', 'topic'}
    assert result['description'] == '' and result['published_at'] is None
    assert result['warning']
    assert result['processing_status'] == 'DRAFT_READY'
    assert len(service.generation_calls) == 1


@pytest.mark.parametrize('published,description', [
    ('2026-04-30', '#cs2 скины'),
    (None, '#cats кошки'),
    ('2026-06-01', '#cats кошки'),
    ('2099-01-01', '#cs2 скины'),
])
def test_known_mismatches_do_not_spend_even_in_simple_flow(service, published, description):
    task = candidate(service, date=published, description=description)
    with pytest.raises(ValidationError):
        service.process_url(task['url'])
    assert service.generation_calls == []
    assert service.db.get_task(task['id'])['processing_status'] == 'SKIPPED'


def test_cancel_between_metadata_and_generation_preserves_queue(service, monkeypatch):
    stop = threading.Event()
    task = candidate(service)
    def progress(message):
        if message == 'Получаем доступные сведения…':
            stop.set()
    result = service.process_url(task['url'], stop_event=stop, progress=progress)
    assert result['cancelled'] is True
    assert service.db.get_task(task['id'])['comment'] == ''
    assert service.generation_calls == []
    stop.clear()
    resumed = service.process_url(task['url'], stop_event=stop)
    assert resumed['id'] == task['id'] and resumed['comment']


def test_cancel_before_import_creates_no_task(service):
    stop = threading.Event(); stop.set()
    assert service.process_url('https://www.tiktok.com/@fixture/video/100', stop_event=stop)['cancelled']
    assert service.stats()['tasks'] == 0


def test_demonstration_returns_visible_result_and_never_changes_real_history(service):
    task = candidate(service)
    before = service.stats()
    result = service.demo_result()
    assert result['demo'] is True and result['is_test'] is True
    assert result['comment'].startswith('тест:')
    assert result['publication_status'] == 'UNCONFIRMED'
    assert service.stats() == before
    assert service.db.get_task(task['id'])['comment'] == ''


def test_old_database_and_settings_reopen_without_schema_reset(service):
    task = candidate(service, date='2026-06-01')
    saved = service.prepare_one(task['id'])
    service.close()
    restored = AppService(service.config_path)
    try:
        row = restored.db.get_task(task['id'])
        assert row['comment'] == saved['comment']
        assert row['created_at'] == saved['created_at']
        assert row['account_id'] == saved['account_id']
        assert restored.process_url(row['url'])['cached'] is True
        with sqlite3.connect(restored.db.path) as conn:
            assert conn.execute('PRAGMA user_version').fetchone()[0] == 1
    finally:
        restored.close()


def test_first_launch_marker_survives_restart_and_can_reopen(service):
    assert not service.onboarding_complete()
    service.complete_onboarding()
    restored = AppService(service.config_path)
    try:
        assert restored.onboarding_complete()
        restored.reset_onboarding()
        assert not service.onboarding_complete()
    finally:
        restored.close()


def test_test_link_rejects_unknown_or_unreviewed_url_before_network(service):
    task = candidate(service)
    for url in [task['url'], 'https://www.tiktok.com/@fixture/video/999']:
        with pytest.raises(ValidationError):
            service.send_test_link(url)
    assert service.stats()['telegram_sent'] == 0
