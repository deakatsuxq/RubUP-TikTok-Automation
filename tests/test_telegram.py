from types import SimpleNamespace

import httpx
import pytest

from api_client import IntegrationError
from telegram_bot import TelegramBot
from validators import ValidationError

TOKEN = "123456:very_secret_token"
URL = "https://www.tiktok.com/@example/video/123456789"


class Outbox:
    def __init__(self):
        self.status = {}
        self.task_updates = []
        self.events = []

    def reserve_telegram(self, url):
        if url in self.status and self.status[url] != "FAILED":
            return False
        self.status[url] = "RESERVED"
        return True

    def finish_telegram(self, url, status, error=""):
        self.status[url] = status
        assert TOKEN not in error

    def update_task(self, task_id, **fields):
        self.task_updates.append((task_id, fields))

    def add_event(self, task_id, kind, detail=""):
        self.events.append((task_id, kind, detail))
        assert TOKEN not in detail


@pytest.fixture
def config():
    return SimpleNamespace(dry_run=False, telegram_mode="draft", timeout=1.0, retries=2)


@pytest.fixture
def task():
    return dict(id=1, url=URL, video_id="123456789", processing_status="DRAFT_READY",
                publication_status="UNCONFIRMED", comment="а что в RubUP?", is_test=False)


def bot(config, handler, db=None):
    return TelegramBot(config, db or Outbox(), client=httpx.Client(transport=httpx.MockTransport(handler)),
                       token=TOKEN, chat_id="-1001234")


def success(request):
    return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})


def test_only_canonical_url_and_exactly_once(config, task):
    requests = []
    def send(request):
        import json
        requests.append(request)
        payload = json.loads(request.content)
        assert payload["text"] == URL
        assert payload["chat_id"] == "-1001234"
        assert "parse_mode" not in payload
        return success(request)
    sender = bot(config, send)
    task["url"] = URL + "?tracking=test#fragment"
    assert sender.send_video(task)
    assert not sender.send_video(task)
    assert len(requests) == 1
    assert sender.db.status[URL] == "SENT"
    assert task["publication_status"] == "UNCONFIRMED"


@pytest.mark.parametrize("mode,status,publication,comment,expected", [
    ("disabled", "DRAFT_READY", "UNCONFIRMED", "RubUP", False),
    ("verified", "NEW", "UNCONFIRMED", "", False),
    ("verified", "ANALYZED", "UNCONFIRMED", "", True),
    ("draft", "ANALYZED", "UNCONFIRMED", "", False),
    ("draft", "DRAFT_READY", "UNCONFIRMED", "", False),
    ("draft", "DRAFT_READY", "UNCONFIRMED", "RubUP", True),
    ("published", "DRAFT_READY", "UNCONFIRMED", "RubUP", False),
    ("published", "PENDING_APPROVAL", "PUBLISHED", "RubUP", True),
])
def test_mode_is_gated_by_actual_stage(config, task, mode, status, publication, comment, expected):
    config.telegram_mode = mode
    task.update(processing_status=status, publication_status=publication, comment=comment)
    sender = bot(config, success)
    assert sender.send_video(task) is expected


@pytest.mark.parametrize("dry,is_test", [(True, False), (False, True), (True, True)])
def test_dry_run_and_test_tasks_never_send(config, task, dry, is_test):
    config.dry_run = dry
    task["is_test"] = is_test
    def forbidden(request):
        pytest.fail("Dry-run attempted real HTTP")
    sender = bot(config, forbidden)
    assert not sender.send_video(task, allow_dry_run=True)
    assert sender.db.status == {}


def test_url_must_match_video_id(config, task):
    sender = bot(config, success)
    task["video_id"] = "999"
    with pytest.raises(ValidationError):
        sender.send_video(task)
    assert sender.db.status == {}


def test_transport_timeout_is_unknown_without_retry_or_secret_leak(config, task):
    seen = []
    def timeout(request):
        seen.append(request)
        raise httpx.ReadTimeout(f"server token={TOKEN}", request=request)
    sender = bot(config, timeout)
    with pytest.raises(IntegrationError) as error:
        sender.send_video(task)
    assert TOKEN not in str(error.value)
    assert sender.db.status[URL] == "UNKNOWN"
    assert not sender.send_video(task)
    assert len(seen) == 1


def test_rate_limit_retries_bounded_then_succeeds(config, task, monkeypatch):
    requests = []
    sleeps = []
    monkeypatch.setattr("telegram_bot.time.sleep", sleeps.append)
    def limited(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, json={"ok": False, "error_code": 429,
                                          "parameters": {"retry_after": 2}})
        return success(request)
    sender = bot(config, limited)
    assert sender.send_video(task)
    assert len(requests) == 2
    assert sleeps == [2]


def test_rate_limit_stops_at_retry_limit(config, task, monkeypatch):
    count = []
    monkeypatch.setattr("telegram_bot.time.sleep", lambda _: None)
    def limited(request):
        count.append(request)
        return httpx.Response(429, json={"ok": False, "error_code": 429})
    sender = bot(config, limited)
    with pytest.raises(IntegrationError):
        sender.send_video(task)
    assert len(count) == 3
    assert sender.db.status[URL] == "FAILED"


def test_server_failure_is_unknown_and_not_retried(config, task):
    count = []
    def failed(request):
        count.append(request)
        return httpx.Response(500, json={"ok": False, "error_code": 500,
                                      "description": TOKEN})
    sender = bot(config, failed)
    with pytest.raises(IntegrationError) as error:
        sender.send_video(task)
    assert TOKEN not in str(error.value)
    assert len(count) == 1
    assert sender.db.status[URL] == "UNKNOWN"


def test_invalid_success_response_does_not_claim_sent(config, task):
    sender = bot(config, lambda _: httpx.Response(200, json={"ok": True, "result": {}}))
    with pytest.raises(IntegrationError):
        sender.send_video(task)
    assert sender.db.status[URL] == "UNKNOWN"


def test_authentication_error_is_sanitized(config, task):
    sender = bot(config, lambda _: httpx.Response(401, json={
        "ok": False, "error_code": 401, "description": TOKEN,
    }))
    with pytest.raises(IntegrationError) as error:
        sender.send_video(task)
    assert TOKEN not in str(error.value)
    assert sender.db.status[URL] == "FAILED"


def test_connection_only_get_me_never_send_message(config):
    paths = []
    def get_me(request):
        paths.append(request.url.path)
        assert request.method == "GET"
        return httpx.Response(200, json={"ok": True, "result": {"is_bot": True, "id": 1}})
    sender = bot(config, get_me)
    assert sender.test_connection()
    assert paths == [f"/bot{TOKEN}/getMe"]
    assert sender.db.status == {}


def test_real_sqlite_outbox_survives_restart(config, tmp_path):
    from database import Database
    from models import Video
    path = tmp_path / "history.sqlite3"
    db = Database(path)
    db.add_video(Video(video_id="123456789", url=URL, published_at="2026-06-01"))
    task = db.ensure_task("account", "123456789")
    db.update_task(task["id"], comment="реклама: что там в RubUP?", processing_status="DRAFT_READY")
    task = db.get_task(task["id"])
    calls = []
    def accepted(request):
        calls.append(request)
        return success(request)
    sender = bot(config, accepted, db=db)
    assert sender.send_video(task)
    db.close()
    restored = Database(path)
    sender_after_restart = bot(config, accepted, db=restored)
    assert not sender_after_restart.send_video(restored.get_task(task["id"]))
    assert len(calls) == 1
    assert restored.get_task(task["id"])["telegram_status"] == "SENT"
    restored.close()


def test_real_sqlite_outbox_blocks_concurrent_duplicate(config, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from database import Database
    from models import Video
    db = Database(tmp_path / "concurrent.sqlite3")
    db.add_video(Video(video_id="123456789", url=URL, published_at="2026-06-01"))
    task = db.ensure_task("account", "123456789")
    db.update_task(task["id"], comment="реклама: что там в RubUP?", processing_status="DRAFT_READY")
    task = db.get_task(task["id"])
    calls = []
    def accepted(request):
        calls.append(request)
        return success(request)
    sender = bot(config, accepted, db=db)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: sender.send_video(task), range(2)))
    assert sorted(results) == [False, True]
    assert len(calls) == 1
    db.close()
