"""RubUP 4 orchestration: no implicit publication or external demo effects."""
from dataclasses import replace
import json
from pathlib import Path
import threading

import httpx
import pytest

from automation_service import AutomationService, _Cancelled
from config import Config, save_config
from models import Video
from validators import ValidationError


class FakeBrowser:
    def __init__(self):
        self.calls = []
        self.error = None
        self.after_fill = None
        self.closed = False

    def open_and_fill(self, slot, url, text, stop_event=None):
        self.calls.append((slot, url, text))
        if self.error:
            raise self.error
        if self.after_fill:
            self.after_fill()
        return {'slot': slot, 'filled': True, 'message': 'Поле заполнено'}

    def open_login(self, slot):
        self.calls.append(('login', slot))
        return {'slot': slot, 'message': 'Войдите самостоятельно'}

    def close(self):
        self.closed = True


@pytest.fixture
def service(tmp_path, monkeypatch):
    for key in ('OPENAI_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'):
        monkeypatch.delenv(key, raising=False)
    def denied(request):
        pytest.fail(f'Unexpected live request to {request.url.host}')
    client = httpx.Client(transport=httpx.MockTransport(denied))
    path = tmp_path / 'Каталог с пробелами' / 'config.yaml'
    save_config(Config(db_path=str(path.parent / 'data' / 'history.sqlite3')), path)
    app = AutomationService(path, browser=FakeBrowser(), client=client)
    yield app
    app.close()
    client.close()


def urls(count=1):
    return '\n'.join(f'https://www.tiktok.com/@fixture/video/{8000000000000000000 + index}' for index in range(count))


def seed(app, count=1):
    app.resume()
    app.bank.import_text('\n'.join(f'RubUP — мой комментарий {index}' for index in range(count * 2)))
    imported = app.import_links(urls(count))
    app.prepare()
    return imported


def complete(app):
    for task in app.tasks():
        if not task['published']:
            app.open_task(task['id'])
            app.confirm_publication(task['id'])
        app.confirm_like(task['id'])


def telegram_client(app, handler):
    app._client = httpx.Client(transport=httpx.MockTransport(handler))
    app._owns_client = True
    app.save_telegram('12345:fixture-token', '-10012345', True)


def test_fifty_urls_have_one_hundred_distinct_bank_assignments_and_no_browser_actions(service):
    imported = seed(service, 50)
    assert imported['added'] == 50 and imported['tasks'] == 100
    tasks = service.tasks()
    assert len({task['comment'] for task in tasks}) == 100
    assert {task['slot'] for task in tasks} == {1, 2}
    assert service.browser.calls == []
    assert service.stats()['remaining'] == service.stats()['used_comments'] == 100
    assert service.stats()['published'] == 0
    assert service.db.usage_summary()['requests'] == 0
    before = [(task['id'], task['comment_id']) for task in tasks]
    assert service.prepare()['assigned'] == 0
    assert [(task['id'], task['comment_id']) for task in service.tasks()] == before


def test_links_are_normalized_duplicates_removed_and_untrusted_inputs_not_echoed(service):
    direct = urls()
    result = service.import_links(direct + '?tracking=secret\n' + direct + '\nhttps://evil.invalid/?secret=leak')
    assert result['added'] == result['duplicates'] == result['rejected'] == 1
    assert result['tasks'] == 2
    assert service.tasks()[0]['url'] == direct
    assert 'secret' not in str(result['errors']) and 'evil' not in str(result['errors'])
    with pytest.raises(ValidationError):
        service.import_links('\n'.join([direct] * 101))
    assert len(service.tasks()) == 2


def test_short_redirects_resolve_only_tiktok_and_invalid_redirect_is_rejected(service):
    requested = []
    def respond(request):
        requested.append(str(request.url))
        target = urls() if 'GOOD' in str(request.url) else 'https://evil.invalid/private'
        return httpx.Response(302, headers={'Location': target})
    client = httpx.Client(transport=httpx.MockTransport(respond))
    service.resolver.client = client
    result = service.import_links('https://vt.tiktok.com/GOOD/\nhttps://vm.tiktok.com/BAD/')
    client.close()
    assert result['added'] == result['rejected'] == 1
    assert requested == ['https://vt.tiktok.com/GOOD/', 'https://vm.tiktok.com/BAD/']


def test_insufficient_comments_remain_waiting_without_duplicating_one_for_both_accounts(service):
    service.bank.import_text('RubUP — только один текст')
    service.import_links(urls())
    result = service.prepare()
    assert result['assigned'] == result['missing'] == 1
    waiting = next(task for task in service.tasks() if not task['comment'])
    with pytest.raises(ValidationError):
        service.open_task(waiting['id'])
    assert service.browser.calls == []


def test_fill_is_not_publication_and_queues_are_independent(service):
    seed(service)
    first, second = service.tasks()
    service.open_task(first['id'])
    assert service.store.get_task(first['id'])['status'] == 'FILLED'
    assert service.stats()['published'] == service.stats()['completed'] == 0
    assert service.store.get_task(second['id'])['status'] == 'READY'
    assert len(service.tasks(first['profile_id'], pending_only=True)) == 1
    with pytest.raises(ValueError):
        service.confirm_like(first['id'])
    service.confirm_publication(first['id'])
    service.confirm_publication(first['id'])  # idempotent explicit confirmation
    assert service.stats()['published'] == 1
    before = list(service.browser.calls)
    assert service.open_task(first['id'])['cached'] is True
    assert service.browser.calls == before
    service.confirm_like(first['id'])
    service.confirm_like(first['id'])
    assert service.stats()['completed'] == 1
    assert len(service.tasks(first['profile_id'], pending_only=True)) == 0
    assert len(service.tasks(second['profile_id'], pending_only=True)) == 1


def test_cancelled_delayed_fill_cannot_enable_confirmation(service):
    seed(service)
    task = service.tasks()[0]
    service.browser.after_fill = service.pause
    result = service.open_task(task['id'])
    assert result['cancelled'] is True and result['filled'] is False
    assert service.store.get_task(task['id'])['status'] == 'READY'
    with pytest.raises(ValueError):
        service.confirm_publication(task['id'])
    assert service.prepare()['cancelled'] is True
    service.resume()
    service.browser.after_fill = None
    assert service.open_task(task['id'])['filled'] is True


def test_combined_cancellation_has_event_wait_protocol():
    primary, external = threading.Event(), threading.Event()
    combined = _Cancelled(primary, external)
    assert combined.wait(0) is False
    external.set()
    assert combined.wait(.1) is True


def test_browser_failure_is_sanitized_and_retry_keeps_bank_comment(service):
    seed(service)
    task = service.tasks()[0]
    service.browser.error = RuntimeError('secret-token browser cookies private url')
    with pytest.raises(ValidationError) as error:
        service.open_task(task['id'])
    assert 'secret-token' not in str(error.value)
    failed = service.store.get_task(task['id'])
    assert failed['status'] == 'FAILED'
    assert failed['comment_id'] == task['comment_id']
    service.browser.error = None
    assert service.open_task(task['id'])['filled'] is True
    assert service.store.get_task(task['id'])['comment_id'] == task['comment_id']


def test_manual_field_fallback_requires_assigned_text_and_separate_publication(service):
    service.import_links(urls())
    task = service.tasks()[0]
    with pytest.raises(ValidationError):
        service.confirm_manual_fill(task['id'])
    service.bank.import_text('RubUP — текст вставлен мной вручную\nRubUP — второй отдельный текст')
    service.prepare()
    filled = service.confirm_manual_fill(task['id'])
    assert filled['status'] == 'FILLED' and filled['published'] is False
    assert service.browser.calls == []
    with service.db._connection() as conn:
        event = conn.execute("SELECT kind FROM v4_events WHERE task_id=? ORDER BY id DESC LIMIT 1", (task['id'],)).fetchone()
    assert event['kind'] == 'MANUAL_FIELD_FILL_CONFIRMED'
    service.confirm_publication(task['id'])
    with pytest.raises(ValidationError):
        service.confirm_manual_fill(task['id'])


def test_partial_account_progress_and_pause_survive_restart(service):
    seed(service)
    first, second = service.tasks()
    service.open_task(first['id'])
    service.confirm_publication(first['id'])
    service.pause()
    restored = AutomationService(service.config_path, browser=FakeBrowser())
    try:
        assert restored.state() == 'PAUSED'
        assert restored.profiles() == service.profiles()
        assert restored.store.get_task(first['id'])['published'] is True
        assert restored.store.get_task(first['id'])['liked'] is False
        assert restored.store.get_task(second['id'])['comment_id'] == second['comment_id']
        assert restored.browser.calls == []
    finally:
        restored.close()


def test_telegram_waits_for_both_accounts_and_sends_only_one_url(service):
    seed(service)
    sent = []
    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 7}})
    telegram_client(service, respond)
    first, second = service.tasks()
    for task in (first, second):
        service.open_task(task['id'])
        service.confirm_publication(task['id'])
    service.confirm_like(first['id'])
    assert service.drain_telegram()['sent'] == 0 and sent == []
    service.confirm_like(second['id'])
    assert service.drain_telegram()['sent'] == 1
    assert sent[0]['text'] == urls() and '\n' not in sent[0]['text']
    assert service.drain_telegram()['sent'] == 0 and len(sent) == 1
    assert service.stats()['telegram_sent'] == 1


def test_uncertain_telegram_attempt_is_not_retried_even_after_restart(service):
    seed(service)
    complete(service)
    calls = []
    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout('private-token-must-not-leak', request=request)
    telegram_client(service, timeout)
    result = service.drain_telegram()
    assert result['unknown'] == 1 and len(calls) == 1
    assert 'private-token' not in str(result['errors'])
    assert service.drain_telegram()['unknown'] == 1 and len(calls) == 1
    restored = AutomationService(service.config_path, browser=FakeBrowser(), client=service._client)
    try:
        restored.resume()
        assert restored.drain_telegram()['unknown'] == 1 and len(calls) == 1
    finally:
        restored.close()


def test_stop_during_telegram_drain_preserves_remaining_eligible_links(service):
    seed(service, 2)
    complete(service)
    calls = []
    def respond(request):
        calls.append(request)
        service.stop()
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 9}})
    telegram_client(service, respond)
    result = service.drain_telegram()
    assert result['sent'] == result['pending'] == 1
    assert len(calls) == 1
    assert service.stats()['telegram_pending'] == 1


def test_new_telegram_delivery_does_not_mutate_historical_task_rows(service):
    video_id = urls().rsplit('/', 1)[-1]
    service.db.add_video(Video(video_id, urls()))
    legacy = service.db.ensure_task('legacy-account', video_id)
    seed(service)
    complete(service)
    telegram_client(service, lambda request: httpx.Response(200, json={'ok': True, 'result': {'message_id': 1}}))
    assert service.drain_telegram()['sent'] == 1
    assert service.db.get_task(legacy['id']) == legacy


def test_existing_telegram_url_reservation_prevents_v4_duplicate_delivery(service):
    service.db.reserve_telegram(urls())
    service.db.finish_telegram(urls(), 'SENT')
    seed(service)
    complete(service)
    telegram_client(service, lambda request: pytest.fail('Already sent URL must not be posted'))
    assert service.drain_telegram()['sent'] == 0
    assert service.stats()['telegram_sent'] == 1


def test_test_mode_isolated_from_working_db_settings_sessions_and_external_network(service):
    seed(service)
    original = service.stats()
    before_config = service.config_path.read_bytes()
    isolated = AutomationService(service.config_path, test_mode=True)
    try:
        assert isolated.root != service.root
        assert Path(isolated.db.path).is_relative_to(isolated.root)
        report = isolated.demo()
        assert report['videos'] == 50 and report['total'] == 100
        assert report['used_comments'] == 100
        assert report['published'] == report['telegram_sent'] == report['api_requests'] == 0
        assert 'отключён' in isolated.open_login(1)['message']
        isolated.save_telegram('123:demo-token', '123', True)
        complete(isolated)
        assert isolated.drain_telegram()['sent'] == 0
        assert isolated.db.usage_summary()['requests'] == 0
    finally:
        isolated.close()
    assert service.stats() == original
    assert service.config_path.read_bytes() == before_config
    assert not (service.root / '.env').exists()


def test_legacy_dry_run_flag_forces_safe_mode_even_without_explicit_test_flag(service):
    safe = AutomationService(service.config_path, config=replace(service.config, dry_run=True))
    try:
        assert safe.test_mode is True
        assert safe.root != service.root
        assert 'отключён' in safe.open_login(2)['message']
    finally:
        safe.close()


def test_explicit_root_anchors_relative_config_and_database(tmp_path):
    isolated_root = tmp_path / 'isolated root'
    app = AutomationService(root=isolated_root)
    try:
        assert app.config_path == isolated_root / 'config.yaml'
        assert Path(app.db.path).is_relative_to(isolated_root)
        assert app.stats()['total'] == 0
    finally:
        app.close()
