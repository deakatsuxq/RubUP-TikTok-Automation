"""Independent cross-version and uncertain-delivery regression checks."""
import httpx

from automation_service import AutomationService
from config import Config, save_config
from models import Video


URL = 'https://www.tiktok.com/@review/video/41123456789'
VIDEO_ID = '41123456789'


class ManualBrowser:
    def open_and_fill(self, slot, url, text, stop_event=None):
        return {'slot': slot, 'filled': True, 'message': 'Local browser double only'}

    def close(self):
        pass


def service_at(root, client):
    path = root / 'config.yaml'
    if not path.exists():
        save_config(Config(db_path=str(root / 'data/history.sqlite3')), path)
    return AutomationService(path, browser=ManualBrowser(), client=client)


def complete_both(service):
    service.resume()
    service.bank.import_text('RubUP — мой первый текст\nRubUP — мой второй текст')
    service.import_links(URL)
    service.prepare()
    for task in service.tasks():
        service.open_task(task['id'])
        service.confirm_publication(task['id'])
        service.confirm_like(task['id'])
    service.save_telegram('123456:local_test_token', '-100123456', True)


def test_successful_v4_delivery_preserves_historical_task_rows(tmp_path):
    requests = []

    def route(request):
        requests.append(request)
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 20}})

    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        service = service_at(tmp_path, client)
        try:
            service.db.add_video(Video(VIDEO_ID, URL))
            legacy = service.db.ensure_task('historical-v2-account', VIDEO_ID)
            before = service.db.get_task(legacy['id'])
            complete_both(service)
            assert service.drain_telegram()['sent'] == 1
            assert service.db.get_task(legacy['id']) == before
            assert len(requests) == 1
            assert requests[0].read().decode().count(URL) == 1
            assert service.db.usage_summary()['requests'] == 0
        finally:
            service.close()


def test_previously_sent_legacy_url_is_not_sent_from_v4_again(tmp_path):
    def forbidden(request):
        raise AssertionError('Existing SENT URL must never be sent again')

    with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
        service = service_at(tmp_path, client)
        try:
            assert service.db.reserve_telegram(URL)
            service.db.finish_telegram(URL, 'SENT')
            complete_both(service)
            assert service.drain_telegram()['sent'] == 0
            assert service.stats()['telegram_sent'] == 1
            assert service.db.telegram_status(URL) == 'SENT'
        finally:
            service.close()


def test_crash_after_reservation_stays_uncertain_across_restart(tmp_path):
    attempted = []

    def route(request):
        attempted.append(request)
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 20}})

    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        first = service_at(tmp_path, client)
        try:
            complete_both(first)
            assert first.db.reserve_telegram(URL)
        finally:
            first.close()
        restored = service_at(tmp_path, client)
        try:
            restored.resume()
            result = restored.drain_telegram()
            assert result['sent'] == 0 and result['unknown'] == 1
            assert attempted == []
            assert restored.stats()['telegram_unknown'] == 1
            assert restored.db.telegram_status(URL) == 'RESERVED'
            assert restored.stats()['completed'] == 2
        finally:
            restored.close()
