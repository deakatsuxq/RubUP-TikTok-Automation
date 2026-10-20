"""Independent cross-module regressions for cancellation and configuration races."""
from dataclasses import replace
import threading

import httpx
import pytest

from application import AppService
from config import Config, save_config
from models import Video
from validators import ValidationError


@pytest.fixture
def service(tmp_path, monkeypatch):
    for name in ('OPENAI_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(httpx.Client, 'request', lambda *a, **k: pytest.fail('unexpected network'))
    path = tmp_path / 'config.yaml'
    save_config(Config(db_path=str(tmp_path / 'history.sqlite3')), path)
    app = AppService(path)
    yield app
    app.close()


def task(service, description='#cs2 скины', published_at=None):
    service.db.add_video(Video('123', 'https://www.tiktok.com/@fixture/video/123',
                              description=description, published_at=published_at))
    return service.db.ensure_task(service.config.account_id, '123')


def test_cancel_at_generation_progress_does_not_start_paid_generation(service, monkeypatch):
    row = task(service)
    stop = threading.Event()
    calls = []
    monkeypatch.setattr(service.generator, 'generate', lambda *args: calls.append(args) or 'реклама: что там в RubUP?')
    def progress(message):
        if message == 'Готовим комментарий…':
            stop.set()
    result = service.process_url(row['url'], stop_event=stop, progress=progress)
    assert calls == []
    assert result['cancelled'] is True
    assert service.db.get_task(row['id'])['comment'] == ''


def test_cancel_at_metadata_progress_does_not_start_oembed(service, monkeypatch):
    row = task(service, description='')
    stop = threading.Event()
    enrich_calls = []
    monkeypatch.setattr(service.search, 'enrich', lambda *args: enrich_calls.append(args))
    monkeypatch.setattr(service.generator, 'generate', lambda *args: pytest.fail('cancelled generation'))
    def progress(message):
        if message == 'Получаем доступные сведения…':
            stop.set()
    result = service.process_url(row['url'], stop_event=stop, progress=progress)
    assert result['cancelled'] is True
    assert enrich_calls == []


def test_settings_cannot_replace_client_during_task_link_delivery(service, monkeypatch):
    row = task(service, published_at='2026-06-01')
    service.config.telegram_mode = 'verified'
    service.db.update_task(row['id'], processing_status='ANALYZED')
    active = service.telegram
    attempted = []
    def send(candidate):
        attempted.append(candidate['id'])
        with pytest.raises(ValidationError):
            service.settings.apply(config=replace(service.config, timeout=7))
        assert service.telegram is active
        return False
    monkeypatch.setattr(active, 'send_video', send)
    assert service.send_task_link(row['id']) is False
    assert attempted == [row['id']]


@pytest.mark.parametrize('method', ['test_openai', 'test_telegram'])
def test_legacy_connection_checks_cannot_race_settings_apply(service, monkeypatch, method):
    client = service.generator if method == 'test_openai' else service.telegram
    calls = []
    def check(probe):
        calls.append(probe)
        with pytest.raises(ValidationError):
            service.settings.apply(config=replace(service.config, timeout=7))
        return True
    monkeypatch.setattr(type(client), 'test_connection', check)
    assert getattr(service, method)() is True
    assert len(calls) == 1


def test_unknown_topic_preliminary_can_only_be_promoted_after_evidence(service, monkeypatch):
    row = task(service, description='', published_at='2026-06-01')
    monkeypatch.setattr(service.search, 'enrich', lambda *args: None)
    monkeypatch.setattr(service.generator, 'generate', lambda *args: 'реклама: что там в RubUP?')
    result = service.process_url(row['url'])
    assert result['review']['missing'] == ['topic']
    assert result['checklist']['preliminary'] is True
    checks = {'published': True, 'comment_present': True, 'comment_not_deleted': True}
    with pytest.raises(ValidationError):
        service.confirm_publication(row['id'], checks)
    service.update_metadata(row['id'], '2026-06-01', '#cs2 проверено вручную')
    assert service.confirm_publication(row['id'], checks)['publication_status'] == 'PUBLISHED'


def test_preliminary_flag_cannot_be_overridden_by_confirmation_payload(service, monkeypatch):
    row = task(service)
    monkeypatch.setattr(service.generator, 'generate', lambda *args: 'реклама: что там в RubUP?')
    service.process_url(row['url'])
    with pytest.raises((ValueError, ValidationError)):
        service.report_tracker.confirm_publication(row['id'], {
            'published': True, 'comment_present': True, 'comment_not_deleted': True,
            'preliminary': False,
        })
    assert service.db.get_task(row['id'])['publication_status'] == 'UNCONFIRMED'
