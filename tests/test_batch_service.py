"""Batch behavior, durable two-account outcomes and no implicit TikTok writes."""
from dataclasses import replace
import threading

import httpx
import pytest

from application import AppService
from config import Config, save_config
from models import Video
from tiktok_search import DiscoveryError
from validators import ValidationError


class Generator:
    def __init__(self):
        self.calls = []
        self.fail_account = None

    def generate(self, task_id, context, budget_guard=None):
        self.calls.append((task_id, context['account_id']))
        if context['account_id'] == self.fail_account:
            raise RuntimeError('provider body must not be exposed')
        slot = context['account_slot']
        return f"реклама: RubUP, обсудим скин {context['video_id']} для примера {slot}?"

    def close(self):
        pass


@pytest.fixture
def app(tmp_path, monkeypatch):
    for key in ('OPENAI_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(httpx.Client, 'request', lambda *a, **k: pytest.fail('unexpected live request'))
    path = tmp_path / 'Папка с пробелами' / 'config.yaml'
    save_config(Config(db_path=str(path.parent / 'data/history.sqlite3')), path)
    service = AppService(path)
    service.generator.close(); service.generator = Generator()
    def unavailable(*args):
        raise DiscoveryError('Недоступно')
    monkeypatch.setattr(service.search, 'enrich', unavailable)
    service.batch._pace = lambda *args: True
    yield service
    service.close()


def seed(app, count=1, date='2026-06-01'):
    urls = []
    for index in range(count):
        video_id = str(8000000000000000000 + index)
        url = f'https://www.tiktok.com/@fixture/video/{video_id}'
        app.db.add_video(Video(video_id, url, description='#cs2 тест метаданных',
                               hashtags=['cs2'], published_at=date))
        urls.append(url)
    return urls


def publication_checks():
    return {'published': True, 'comment_present': True, 'comment_not_deleted': True}


def test_fifty_urls_create_one_hundred_account_pairs_and_resume_without_paid_repeats(app):
    urls = seed(app, 50)
    imported = app.batch.import_links('\n'.join(urls))
    assert imported['accepted'] == 50 and imported['duplicates'] == imported['invalid'] == 0
    batch_id = imported['batch_id']
    assert len(app.batch.groups(batch_id)) == 50
    assert app.batch.estimate(batch_id)['requests'] == 100
    result = app.batch.prepare(batch_id)
    assert result['prepared'] == 100 and result['failed'] == 0
    summary = app.batch.summary(batch_id)
    assert summary['videos_total'] == summary['videos_ready'] == 50
    assert summary['comments_total'] == summary['comments_ready'] == 100
    assert summary['published_total'] == summary['telegram_sent'] == 0
    comments = [row['comment'] for row in app.db.list_tasks()]
    assert len(set(comments)) == 100
    assert len(app.generator.calls) == 100
    assert app.batch.prepare(batch_id)['prepared'] == 0
    assert len(app.generator.calls) == 100
    restored = AppService(app.config_path)
    try:
        assert restored.batch.summary(batch_id)['comments_ready'] == 100
        assert restored.batch.estimate(batch_id)['requests'] == 0
        assert restored.batch.prepare(batch_id)['prepared'] == 0
        assert restored.batch.profiles.list()[0]['profile_id'] == app.config.account_id
    finally:
        restored.close()


def test_import_counts_duplicate_invalid_and_canonical_alias(app):
    url = seed(app)[0]
    result = app.batch.import_links(url + '\n' + url + '?share=1\nhttps://localhost/video/1')
    assert result['accepted'] == 1 and result['duplicates'] == 1 and result['invalid'] == 1
    assert len(app.batch.groups(result['batch_id'])) == 1
    assert len(app.db.list_tasks()) == 2


def test_short_urls_are_saved_before_resolution_and_share_real_video_pair(app, monkeypatch):
    url = seed(app)[0]
    imported = app.batch.import_links('https://vt.tiktok.com/AAAA/\nhttps://vm.tiktok.com/BBBB/')
    items = app.batch.store.list_items(imported['batch_id'])
    assert len(items) == 2 and all(item['video_id'] is None for item in items)
    calls = []
    def resolve(value):
        calls.append(value)
        return url, url.rsplit('/', 1)[-1]
    monkeypatch.setattr(app.search, 'resolve_url', resolve)
    result = app.batch.prepare(imported['batch_id'])
    assert result['prepared'] == 2
    assert len(calls) == 2
    assert len(app.batch.groups(imported['batch_id'])) == 1
    assert len(app.db.list_tasks()) == 2


def test_failed_second_account_retry_does_not_repeat_successful_first(app):
    batch_id = app.batch.import_links(seed(app)[0])['batch_id']
    profiles = app.batch.profiles.list()
    app.generator.fail_account = profiles[1]['profile_id']
    first = app.batch.prepare(batch_id)
    pair = app.batch.groups(batch_id)[0]['tasks']
    assert first['prepared'] == first['failed'] == 1
    assert pair[0]['comment'] and not pair[1]['comment']
    assert pair[1]['processing_status'] == 'FAILED' and 'provider body' not in pair[1]['error']
    assert app.batch.prepare(batch_id)['prepared'] == 0
    assert len(app.generator.calls) == 2
    app.generator.fail_account = None
    app.batch.retry_task(pair[1]['id'])
    assert len(app.generator.calls) == 3
    assert all(task['comment'] for task in app.batch.groups(batch_id)[0]['tasks'])


def test_published_pair_is_not_generated_again_when_reimported(app):
    url = seed(app)[0]
    first = app.batch.import_links(url)['batch_id']
    app.batch.prepare(first)
    pair = app.batch.groups(first)[0]['tasks']
    app.batch.confirm_publication(pair[0]['id'], publication_checks())
    second = app.batch.import_links(url)['batch_id']
    assert app.batch.prepare(second)['prepared'] == 0
    assert len(app.generator.calls) == 2
    assert app.batch.groups(second)[0]['tasks'][0]['publication_status'] == 'PUBLISHED'
    with pytest.raises(ValidationError):
        app.batch.regenerate(pair[0]['id'])


def test_separate_publication_like_states_gate_telegram_both_accounts(app):
    app.config.require_like = True
    batch_id = app.batch.import_links(seed(app)[0])['batch_id']
    app.batch.prepare(batch_id)
    pair = app.batch.groups(batch_id)[0]['tasks']
    app.batch.confirm_publication(pair[0]['id'], publication_checks())
    assert app.db.get_task(pair[1]['id'])['publication_status'] == 'UNCONFIRMED'
    assert app.batch.store.telegram_queue() == []
    app.batch.confirm_publication(pair[1]['id'], publication_checks())
    assert app.batch.store.telegram_queue() == []
    app.batch.confirm_like(pair[0]['id'])
    assert app.batch.store.telegram_queue() == []
    app.batch.confirm_like(pair[1]['id'])
    queue = app.batch.store.telegram_queue()
    assert len(queue) == 1 and queue[0]['status'] == 'PENDING'
    assert app.batch.summary(batch_id)['published_total'] == 2
    assert app.batch.summary(batch_id)['telegram_sent'] == 0


def test_pending_like_and_telegram_state_survive_restart(app):
    app.config.require_like = True
    batch_id = app.batch.import_links(seed(app)[0])['batch_id']
    app.batch.prepare(batch_id)
    pair = app.batch.groups(batch_id)[0]['tasks']
    for task in pair:
        app.batch.confirm_publication(task['id'], publication_checks())
    app.batch.confirm_like(pair[0]['id'])
    restored = AppService(app.config_path)
    try:
        tasks = restored.batch.groups(batch_id)[0]['tasks']
        assert tasks[0]['checklist']['own_comment_liked'] is True
        assert not tasks[1]['checklist'].get('own_comment_liked')
        restored.batch.confirm_like(tasks[1]['id'])
        assert restored.batch.store.telegram_queue()[0]['status'] == 'PENDING'
    finally:
        restored.close()


def test_unknown_metadata_allows_preliminary_but_no_confirmation(app):
    imported = app.batch.import_links('https://www.tiktok.com/@fixture/video/123')
    app.batch.prepare(imported['batch_id'])
    group = app.batch.groups(imported['batch_id'])[0]
    assert all(task['checklist']['preliminary'] for task in group['tasks'])
    with pytest.raises(ValidationError):
        app.batch.confirm_publication(group['tasks'][0]['id'], publication_checks())
    app.batch.update_metadata('123', '2026-06-01', '#cs2 проверено')
    updated = app.batch.groups(imported['batch_id'])[0]
    assert all(not task['checklist']['preliminary'] for task in updated['tasks'])
    assert len(app.generator.calls) == 2


def test_known_mismatch_skips_both_accounts_without_generation(app):
    imported = app.batch.import_links(seed(app, date='2026-04-01')[0])
    result = app.batch.prepare(imported['batch_id'])
    assert result['skipped'] == 1
    assert app.generator.calls == []
    assert all(task['processing_status'] == 'SKIPPED' for task in app.batch.groups(imported['batch_id'])[0]['tasks'])


def test_stop_keeps_partially_prepared_pair_for_resume(app):
    batch_id = app.batch.import_links(seed(app, 2)[0])['batch_id']
    stop = threading.Event()
    original = app.generator.generate
    def generate(*args, **kwargs):
        result = original(*args, **kwargs)
        stop.set()
        return result
    app.generator.generate = generate
    result = app.batch.prepare(batch_id, stop_event=stop)
    assert result['stopped'] is True and result['prepared'] == 1
    assert app.batch.summary(batch_id)['comments_ready'] == 1
    stop.clear(); app.generator.generate = original
    assert app.batch.prepare(batch_id)['prepared'] == 1
    assert len(app.generator.calls) == 2


def test_no_paid_call_when_required_image_is_missing(app):
    app.config.require_image = True; app.config.image_mode = 'specific'; app.config.selected_image = 4
    batch_id = app.batch.import_links(seed(app)[0])['batch_id']
    result = app.batch.prepare(batch_id)
    assert result['failed'] == 2 and app.generator.calls == []
    assert all(task['processing_status'] == 'FAILED' and task['error'] for task in app.batch.groups(batch_id)[0]['tasks'])


def test_publication_of_one_account_protects_shared_metadata(app):
    batch_id = app.batch.import_links(seed(app)[0])['batch_id']
    app.batch.prepare(batch_id)
    pair = app.batch.groups(batch_id)[0]['tasks']
    app.batch.confirm_publication(pair[0]['id'], publication_checks())
    with pytest.raises(ValidationError):
        app.batch.update_metadata(pair[0]['video_id'], None, '#cats changed')
    assert app.db.get_task(pair[1]['id'])['published_at'] == '2026-06-01'


def test_demo_fifty_videos_one_hundred_drafts_without_production_changes(app):
    seed(app)
    before = app.stats()
    before_profiles = app.batch.profiles.list()
    demo = app.batch.demo()
    assert demo['is_test'] is True and demo['demo'] is True
    assert len(demo['groups']) == 50
    assert demo['summary']['comments_ready'] == 100
    assert demo['summary']['published_total'] == demo['summary']['telegram_sent'] == 0
    assert len({task['comment'] for group in demo['groups'] for task in group['tasks']}) == 100
    assert all(task['is_test'] for group in demo['groups'] for task in group['tasks'])
    assert app.stats() == before and app.batch.profiles.list() == before_profiles


_HTTP_REQUEST = httpx.Client.request


def mocked_client(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    # The global fixture forbids real HTTP. This instance can call only the
    # explicit MockTransport used to exercise the real Telegram/OpenAI adapter.
    client.request = lambda *args, **kwargs: _HTTP_REQUEST(client, *args, **kwargs)
    return client


def telegram_adapter(app, monkeypatch, handler):
    import batch_service
    from telegram_bot import TelegramBot
    app.credential_store.update({'TELEGRAM_BOT_TOKEN': '123:test_token', 'TELEGRAM_CHAT_ID': '42'})
    client = mocked_client(handler)
    monkeypatch.setattr(batch_service, 'TelegramBot', lambda config, db, **keys:
                        TelegramBot(config, db, client=client, **keys))
    return client


def prepared_pair(app):
    batch_id = app.batch.import_links(seed(app)[0])['batch_id']
    app.batch.prepare(batch_id)
    pair = app.batch.groups(batch_id)[0]['tasks']
    for task in pair:
        app.batch.confirm_publication(task['id'], publication_checks())
    return batch_id, pair


def test_real_telegram_adapter_sends_only_one_url_once_for_both_accounts(app, monkeypatch):
    import json
    batch_id, pair = prepared_pair(app)
    calls = []
    def handle(request):
        calls.append(request)
        payload = json.loads(request.content)
        assert payload['text'] == pair[0]['url']
        assert '\n' not in payload['text'] and 'RubUP' not in payload['text']
        assert payload['chat_id'] == '42'
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 17}})
    client = telegram_adapter(app, monkeypatch, handle)
    try:
        assert app.batch.drain_telegram(batch_id)['sent'] == 1
        assert app.batch.drain_telegram(batch_id)['sent'] == 0
        assert len(calls) == 1
        assert app.batch.summary(batch_id)['telegram_sent'] == 1
        assert all(task['telegram_status'] == 'SENT' for task in app.batch.groups(batch_id)[0]['tasks'])
    finally:
        client.close()


def test_unknown_delivery_remains_blocked_without_automatic_second_send(app, monkeypatch):
    batch_id, pair = prepared_pair(app)
    calls = []
    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout('transport included token; never log it')
    client = telegram_adapter(app, monkeypatch, timeout)
    try:
        assert app.batch.drain_telegram(batch_id)['unknown'] == 1
        assert app.batch.store.telegram_queue(batch_id)[0]['status'] == 'UNKNOWN'
        assert app.db.telegram_status(pair[0]['url']) == 'UNKNOWN'
        assert app.batch.drain_telegram(batch_id)['sent'] == 0
        assert len(calls) == 1
        assert app.batch.summary(batch_id)['telegram_sent'] == 0
    finally:
        client.close()


def test_configured_any_mode_can_queue_only_complete_first_account(app):
    app.config.batch_telegram_mode = 'any'
    batch_id = app.batch.import_links(seed(app)[0])['batch_id']
    app.batch.prepare(batch_id)
    pair = app.batch.groups(batch_id)[0]['tasks']
    app.batch.confirm_publication(pair[0]['id'], publication_checks())
    assert app.batch.store.telegram_queue(batch_id)[0]['status'] == 'PENDING'
    assert app.db.get_task(pair[1]['id'])['publication_status'] == 'UNCONFIRMED'


def test_finite_cost_limit_with_unknown_tariffs_stops_before_real_api_request(app):
    from ai_comment_generator import CommentGenerator
    app.config.batch_budget_usd = 1
    calls = []
    client = mocked_client(lambda request: calls.append(request) or httpx.Response(500))
    app.generator = CommentGenerator(app.config, app.db, client=client, api_key='test-key')
    try:
        batch_id = app.batch.import_links(seed(app)[0])['batch_id']
        result = app.batch.prepare(batch_id)
        assert result['budget_blocked'] is True
        assert result['prepared'] == 0 and calls == []
        assert app.db.usage_summary()['requests'] == 0
        assert app.batch.store.get_batch(batch_id)['state'] == 'STOPPED'
    finally:
        client.close()


def test_unknown_openai_delivery_stops_later_account_and_remains_blocked_on_retry(app):
    from ai_comment_generator import CommentGenerator
    calls = []
    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout('Unknown response')
    client = mocked_client(timeout)
    app.generator = CommentGenerator(app.config, app.db, client=client, api_key='test-key')
    try:
        batch_id = app.batch.import_links(seed(app)[0])['batch_id']
        result = app.batch.prepare(batch_id)
        assert result['failed'] == 1 and result['budget_blocked'] is True
        assert len(calls) == 1
        assert app.batch.summary(batch_id)['budget']['unknown_requests'] == 1
        app.batch.prepare(batch_id, retry_failed=True)
        assert len(calls) == 1
        assert app.db.usage_summary()['total_cost'] is None
    finally:
        client.close()


def test_corrected_manual_evidence_unblocks_skipped_video_without_reimport(app):
    imported = app.batch.import_links(seed(app, date='2026-04-01')[0])
    batch_id = imported['batch_id']
    assert app.batch.estimate(batch_id)['requests'] == 0
    assert app.batch.prepare(batch_id)['skipped'] == 1
    item = app.batch.groups(batch_id)[0]
    app.batch.update_metadata(item['video_id'], '2026-06-01', '#cs2 дата исправлена вручную')
    assert app.batch.store.get_item(item['item_id'])['state'] == 'RESOLVED'
    assert app.batch.estimate(batch_id)['requests'] == 2
    assert app.batch.prepare(batch_id)['prepared'] == 2
    assert len(app.generator.calls) == 2


def test_automatic_enrichment_does_not_modify_metadata_after_one_publication(app, monkeypatch):
    video_id = '8000000000000000000'
    url = f'https://www.tiktok.com/@fixture/video/{video_id}'
    app.db.add_video(Video(video_id, url, description='', hashtags=['cs2'], published_at='2026-06-01'))
    batch_id = app.batch.import_links(url)['batch_id']
    pair = app.batch.groups(batch_id)[0]['tasks']
    app.batch.save_comment(pair[0]['id'], 'реклама: что там в RubUP?')
    app.batch.confirm_publication(pair[0]['id'], publication_checks())
    monkeypatch.setattr(app.search, 'enrich', lambda *args: pytest.fail('Published metadata is immutable'))
    assert app.batch.prepare(batch_id)['prepared'] == 1
    assert app.db.get_video(video_id)['description'] == ''
    assert app.db.get_video(video_id)['hashtags'] == ['cs2']
