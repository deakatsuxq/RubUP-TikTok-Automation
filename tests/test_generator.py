import json

import httpx
import pytest

from ai_comment_generator import CommentGenerator
from api_client import APIClient, IntegrationError
from config import Config
from database import Database
from models import Video
from validators import ValidationError


def task_db(tmp_path, number="123"):
    db = Database(tmp_path / "history.sqlite3")
    db.add_video(Video(number, f"https://www.tiktok.com/@test/video/{number}", description="про CS2", hashtags=["cs2"], published_at="2026-05-02"))
    return db, db.ensure_task("account", number)


def response(text, **extras):
    return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": 100, "output_tokens": 10}, **extras}


def test_responses_api_payload_context_and_real_usage(tmp_path):
    db, task = task_db(tmp_path)
    requests = []
    def route(request):
        requests.append(request)
        body = json.loads(request.content)
        assert request.url == "https://api.openai.com/v1/responses"
        assert body["model"] == "gpt-4.1-mini"
        assert body["store"] is False
        context = json.loads(body["input"])
        assert context["description"] == "про CS2"
        assert context["category"] == "CS2"
        assert "confirmed_personal_experience" in context["requirements"]
        return httpx.Response(200, json=response("реклама: RubUP, а какой скин выбрать?"))
    config = Config(input_price_per_million=1, output_price_per_million=2)
    generator = CommentGenerator(config, db, httpx.Client(transport=httpx.MockTransport(route)), api_key="secret")
    value = generator.generate(task["id"], {**db.get_video("123"), "category": "CS2"})
    assert "RubUP" in value
    assert len(requests) == 1
    assert db.usage_summary()["input_tokens"] == 100
    assert db.get_task(task["id"])["comment"] == ""  # Caller saves draft, generation is not publication.


def test_missing_brand_regenerates_and_records_paid_reject(tmp_path):
    db, task = task_db(tmp_path)
    comments = iter(["просто вопрос", "реклама: что думаете о RubUP?"])
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response(next(comments)))))
    generator = CommentGenerator(Config(), db, client, "key")
    assert "RubUP" in generator.generate(task["id"], db.get_video("123"))
    assert db.usage_summary()["requests"] == 2
    assert db.get_task(task["id"])["attempts"] == 2


def test_initial_plus_two_retries_skip_no_publication(tmp_path):
    db, task = task_db(tmp_path)
    count = 0
    def route(request):
        nonlocal count
        count += 1
        return httpx.Response(200, json=response("без упоминания"))
    generator = CommentGenerator(Config(), db, httpx.Client(transport=httpx.MockTransport(route)), "key")
    with pytest.raises(ValidationError, match="3 попытки"):
        generator.generate(task["id"], db.get_video("123"))
    current = db.get_task(task["id"])
    assert count == 3
    assert current["processing_status"] == "SKIPPED"
    assert current["publication_status"] != "PUBLISHED"
    assert db.usage_summary()["requests"] == 3


def test_global_duplicate_regenerated_even_beyond_recent_list(tmp_path):
    db, task = task_db(tmp_path)
    db.add_video(Video("456", "https://www.tiktok.com/@test/video/456"))
    other = db.ensure_task("account", "456")
    db.update_task(other["id"], comment="реклама: RubUP, какой скин выбрать?")
    comments = iter(["Реклама: RUBUP, какой скин выбрать!", "реклама: в рубапе есть что обсудить?"])
    generator = CommentGenerator(Config(), db, httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response(next(comments))))), "key")
    assert generator.generate(task["id"], db.get_video("123")) == "реклама: в рубапе есть что обсудить?"
    assert db.get_task(task["id"])["attempts"] == 2


def test_no_invented_thanks_or_personal_win(tmp_path):
    db, task = task_db(tmp_path)
    comments = iter(["реклама: RubUP, спс за скин", "реклама: я выиграл в RubUP", "реклама: что там в RubUP?"])
    generator = CommentGenerator(Config(), db, httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response(next(comments))))), "key")
    assert generator.generate(task["id"], db.get_video("123")) == "реклама: что там в RubUP?"
    assert db.usage_summary()["requests"] == 3


def test_dry_run_never_calls_network_or_claims_paid_usage(tmp_path):
    db, task = task_db(tmp_path)
    def forbidden(request):
        pytest.fail("dry-run attempted a network request")
    generator = CommentGenerator(Config(dry_run=True), db, httpx.Client(transport=httpx.MockTransport(forbidden)))
    assert generator.generate(task["id"], db.get_video("123")).startswith("тест:")
    assert db.usage_summary()["requests"] == 0


def test_missing_key_never_falls_back_to_fake_generator(tmp_path, monkeypatch):
    db, task = task_db(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    generator = CommentGenerator(Config(), db)
    with pytest.raises(IntegrationError, match="OPENAI_API_KEY"):
        generator.generate(task["id"], db.get_video("123"))
    generator.close()


def test_api_failure_is_sanitized_no_unbounded_mutation_retry(tmp_path):
    db, task = task_db(tmp_path)
    calls = []
    def route(request):
        calls.append(request)
        raise httpx.ReadTimeout("private-key-secret " + str(request.url))
    generator = CommentGenerator(Config(retries=5), db, httpx.Client(transport=httpx.MockTransport(route)), "private-key-secret")
    with pytest.raises(IntegrationError) as exc:
        generator.generate(task["id"], db.get_video("123"))
    assert "private-key-secret" not in str(exc.value)
    assert len(calls) == 1
    assert "private-key-secret" not in str(db.events())


def test_read_connection_retries_explicit_failures_and_verifies_model():
    responses = iter([httpx.Response(503), httpx.Response(200, json={"data": [{"id": "gpt-4.1-mini"}]})])
    generator = CommentGenerator(Config(), None, httpx.Client(transport=httpx.MockTransport(lambda r: next(responses))), "key")
    generator.http.sleep = lambda _: None
    assert generator.test_connection()


def test_api_429_bounded_and_retry_after_respected():
    sleeps, calls = [], []
    def route(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "3"}, json={"error": {"message": "secret"}})
    helper = APIClient(httpx.Client(transport=httpx.MockTransport(route)), retries=2, sleep=sleeps.append)
    with pytest.raises(IntegrationError, match="429"):
        helper.request("POST", "https://api.openai.com/v1/responses")
    assert len(calls) == 3
    assert sleeps == [3, 3]


def test_long_cooldown_stops_instead_of_retrying_early():
    helper = APIClient(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "120"}))), sleep=lambda _: pytest.fail("must pause"))
    with pytest.raises(IntegrationError, match="паузу"):
        helper.request("POST", "https://api.openai.com/v1/responses")


def test_missing_usage_is_unknown_not_free(tmp_path):
    db, task = task_db(tmp_path)
    data = response("реклама: что там в RubUP?", usage=None)
    generator = CommentGenerator(Config(input_price_per_million=1, output_price_per_million=1), db, httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data))), "key")
    generator.generate(task["id"], db.get_video("123"))
    summary = db.usage_summary()
    assert summary["requests"] == 1
    assert any(event["kind"] == "OPENAI_USAGE_UNKNOWN" for event in db.events())


def test_complete_json_context_is_bounded_and_still_contains_requirements():
    generator = CommentGenerator(Config(max_context_chars=512), None, api_key="key")
    context = generator._context({"description": "текст " * 10000, "hashtags": ["cs2"] * 100,
                                  "category": "CS2", "query": "very long search" * 20},
                                 ["реклама RubUP и старый комментарий"] * 30)
    assert len(context) <= 512
    parsed = json.loads(context)
    assert parsed["requirements"]["advertising"] is True
    assert parsed["requirements"]["confirmed_personal_experience"] is False
    generator.close()


def test_unknown_transport_attempt_is_recorded_as_unknown_cost(tmp_path):
    db, task = task_db(tmp_path)
    def route(request):
        raise httpx.ReadTimeout("private")
    generator = CommentGenerator(Config(), db, httpx.Client(transport=httpx.MockTransport(route)), "key")
    with pytest.raises(IntegrationError):
        generator.generate(task["id"], db.get_video("123"))
    summary = db.usage_summary()
    assert summary["requests"] == 1
    assert any(event["kind"] == "OPENAI_USAGE_UNKNOWN" for event in db.events())


def test_rate_limit_rejections_count_actual_http_requests(tmp_path):
    db, task = task_db(tmp_path)
    responses = iter([httpx.Response(429), httpx.Response(200, json=response("реклама: что там в RubUP?"))])
    generator = CommentGenerator(Config(), db, httpx.Client(transport=httpx.MockTransport(lambda r: next(responses))), "key")
    generator.http.sleep = lambda _: None
    generator.generate(task["id"], db.get_video("123"))
    assert db.usage_summary()["requests"] == 2


def test_incomplete_output_never_becomes_ready_draft(tmp_path):
    db, task = task_db(tmp_path)
    data = response("реклама: что там в RubUP?", status="incomplete")
    generator = CommentGenerator(Config(), db, httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data))), "key")
    with pytest.raises(ValidationError):
        generator.generate(task["id"], db.get_video("123"))
    assert db.get_task(task["id"])["processing_status"] == "SKIPPED"
    assert db.usage_summary()["requests"] == 3
