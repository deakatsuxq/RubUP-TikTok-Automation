"""Independent batch regressions: evidence, crash recovery and private state."""
from pathlib import Path

import httpx
import pytest

from application import AppService
from batch_service import BatchService
from budget import BudgetGuard
from config import Config, save_config
from models import Video
from validators import ValidationError


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(httpx.Client, 'request', lambda *a, **k: pytest.fail('unexpected live network'))
    path = tmp_path / 'config.yaml'
    save_config(Config(db_path=str(tmp_path / 'data/history.sqlite3'), target_per_minute=60), path)
    service = AppService(path)
    yield service
    service.close()


def prepared(app, number='123', require_like=False):
    app.config.require_like = require_like
    service = app.batch
    imported = service.import_links(f'https://www.tiktok.com/@fixture/video/{number}')
    service.update_metadata(number, '2026-06-01', '#cs2 проверенные скины')
    group = service.groups(imported['batch_id'])[0]
    comments = ['реклама: что думаете о RubUP?', 'реклама: в рубапе какой скин выбрать?']
    for task, comment in zip(group['tasks'], comments):
        service.save_comment(task['id'], comment)
    return service, imported['batch_id'], service.groups(imported['batch_id'])[0]


def confirmations():
    return {'published': True, 'comment_present': True, 'comment_not_deleted': True}


def test_both_accounts_and_both_required_likes_gate_one_url(app):
    service, batch_id, group = prepared(app, require_like=True)
    first, second = group['tasks']
    service.confirm_publication(first['id'], confirmations())
    assert service.store.telegram_queue(batch_id) == []
    service.confirm_publication(second['id'], confirmations())
    assert service.store.telegram_queue(batch_id) == []
    service.confirm_like(first['id'])
    assert service.store.telegram_queue(batch_id) == []
    service.confirm_like(second['id'])
    rows = service.store.telegram_queue(batch_id)
    assert len(rows) == 1 and rows[0]['status'] == 'PENDING'
    assert rows[0]['url'] == group['url']
    assert service.summary(batch_id)['published_total'] == 2
    assert service.summary(batch_id)['telegram_sent'] == 0


def test_revoked_metadata_prevents_previously_queued_link_delivery(app, monkeypatch):
    app.config.batch_telegram_mode = 'verified'
    service, batch_id, group = prepared(app)
    service.queue_eligible(batch_id)
    assert service.store.telegram_queue(batch_id)[0]['status'] == 'PENDING'
    service.update_metadata('123', None, '#cs2 дату нужно проверить')
    sent = []
    monkeypatch.setattr('batch_service.TelegramBot.send_video', lambda self, task: sent.append(task) or True)
    result = service.drain_telegram(batch_id)
    assert sent == []
    assert result['sent'] == 0 and result['skipped'] == 1
    assert service.store.telegram_queue(batch_id)[0]['status'] == 'PENDING'


def test_unknown_delivery_is_not_automatically_retried(app, monkeypatch):
    app.config.batch_telegram_mode = 'verified'
    service, batch_id, group = prepared(app)
    service.queue_eligible(batch_id)
    assert app.db.reserve_telegram(group['url'])
    app.db.finish_telegram(group['url'], 'UNKNOWN', 'answer lost')
    sent = []
    monkeypatch.setattr('batch_service.TelegramBot.send_video', lambda self, task: sent.append(task) or True)
    service.drain_telegram(batch_id)
    service.drain_telegram(batch_id)
    assert sent == []
    assert service.store.telegram_queue(batch_id)[0]['status'] == 'UNKNOWN'


def test_crash_after_confirmed_send_recovers_positive_evidence(app, monkeypatch):
    app.config.batch_telegram_mode = 'verified'
    service, batch_id, group = prepared(app)
    service.queue_eligible(batch_id)
    service.store.update_telegram(group['url'], 'IN_FLIGHT')
    assert app.db.reserve_telegram(group['url'])
    app.db.finish_telegram(group['url'], 'SENT')
    sent = []
    monkeypatch.setattr('batch_service.TelegramBot.send_video', lambda self, task: sent.append(task) or True)
    service.drain_telegram(batch_id)
    assert sent == []
    assert service.store.telegram_queue(batch_id)[0]['status'] == 'SENT'


def test_missing_original_records_failed_task_without_spending(app, monkeypatch):
    app.config.require_image = True
    app.config.image_mode = 'specific'
    service = app.batch
    imported = service.import_links('https://www.tiktok.com/@fixture/video/123')
    service.update_metadata('123', '2026-06-01', '#cs2 проверенные скины')
    calls = []
    monkeypatch.setattr(app.generator, 'generate', lambda *a, **k: calls.append(a) or 'реклама: что там в RubUP?')
    result = service.prepare(imported['batch_id'])
    tasks = service.groups(imported['batch_id'])[0]['tasks']
    assert calls == [] and result['failed'] == 2
    assert all(task['processing_status'] == 'FAILED' and task['error'] for task in tasks)
    assert service.summary(imported['batch_id'])['failed'] >= 2


def test_unknown_manual_tariff_does_not_become_zero_total_cost(app):
    service = app.batch
    imported = service.import_links('https://www.tiktok.com/@fixture/video/123')
    guard = BudgetGuard(app.db, imported['batch_id'], app.config)
    token = guard.before_request('контекст', 'инструкция', 20)
    guard.settle(token, None, usage_known=True)
    summary = service.summary(imported['batch_id'])
    assert summary['budget']['total_cost_known'] is False
    assert summary['total_cost'] is None


def test_task_outside_batch_cannot_be_published_then_fail_lookup(app):
    service = app.batch
    profile = service.profiles.list()[0]
    app.db.add_video(Video('100', 'https://www.tiktok.com/@fixture/video/100',
                           description='#cs2 скины', published_at='2026-06-01'))
    task = app.db.ensure_task(profile['profile_id'], '100')
    app.db.update_task(task['id'], comment='реклама: что думаете о RubUP?', processing_status='PENDING_APPROVAL',
                       checklist={'preliminary': False})
    with pytest.raises((ValueError, ValidationError)):
        service.confirm_publication(task['id'], confirmations())
    assert app.db.get_task(task['id'])['publication_status'] == 'UNCONFIRMED'


def test_demo_cannot_touch_production_when_storage_name_collides(tmp_path, monkeypatch):
    monkeypatch.setattr(httpx.Client, 'request', lambda *a, **k: pytest.fail('unexpected live network'))
    path = tmp_path / 'config.yaml'
    save_config(Config(db_path=str(tmp_path / 'data/batch_demo.sqlite3')), path)
    service = None
    try:
        try:
            service = AppService(path)
        except ValueError:
            return  # Rejecting the reserved name up front is a safe implementation.
        service.db.add_video(Video('100', 'https://www.tiktok.com/@fixture/video/100',
                                   description='#cs2 скины', published_at='2026-06-01'))
        before = service.db.stats()
        service.batch.demo()
        assert service.db.stats() == before
    finally:
        if service:
            service.close()
