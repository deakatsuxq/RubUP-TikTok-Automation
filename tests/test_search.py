from dataclasses import replace
from datetime import date, timedelta
import json

import httpx
import pytest

from config import Config
from database import Database
from models import Video
from tiktok_search import DiscoveryError, SearchService
from video_analyzer import analyze_video, extract_hashtags


def video(video_id="111", **kwargs):
    return Video(video_id=video_id, url=f"https://www.tiktok.com/@fixture/video/{video_id}", is_test=True, **kwargs)


def test_matching_exact_hashtags_keywords_and_disabled_tags():
    config = Config()
    assert analyze_video(video(description="#CS2 норм", published_at="2026-05-01"), config)["eligible"]
    assert not analyze_video(video(description="#cs2clips", published_at="2026-05-01"), config)["eligible"]
    assert not analyze_video(video(description="#CS2", published_at="2026-05-01"), replace(config, disabled_hashtags=["cs2"]))["eligible"]
    assert analyze_video(video(description="редкие предметы", published_at="2026-05-01"), replace(config, keywords=["редкие"]))["eligible"]
    assert extract_hashtags("#CS2 #cs2 #тгподарки привет") == ["cs2", "тгподарки"]


def test_date_lower_boundary_unknown_and_future():
    config = Config()
    assert not analyze_video(video(description="#rubup", published_at="2026-04-30"), config)["eligible"]
    assert analyze_video(video(description="#rubup", published_at="2026-05-01"), config)["eligible"]
    for value in (None, "unknown", "2026-02-30", (date.today() + timedelta(days=1)).isoformat()):
        result = analyze_video(video(description="#rubup", published_at=value), config)
        assert result["needs_review"]
        assert not result["eligible"]


def test_category_dota_does_not_become_cs2():
    assert analyze_video(video(description="#dota2 #скины", published_at="2026-05-01"), Config())["category"] == "Dota 2"


def test_plain_import_dedup_invalid_and_manual_queue(tmp_path):
    db = Database(tmp_path / "history.sqlite3")
    service = SearchService(Config(), db)
    counts = service.import_urls("https://www.tiktok.com/@fixture/video/101\nhttps://www.tiktok.com/@fixture/video/101?x=1\nhttps://evil.example/a")
    assert counts["added"] == 1
    assert counts["duplicate"] == 1
    assert counts["rejected"] == 1
    assert counts["manual_review"] == 1
    results = service.search()
    assert len(results) == 1
    assert results[0]["needs_review"] and not results[0]["eligible"]
    assert db.get_video("101")["author"] == ""
    assert db.get_video("101")["published_at"] is None
    service.close()
    db.close()


def test_csv_json_import_filtering_and_processed_exclusion(tmp_path):
    db = Database(tmp_path / "history.sqlite3")
    service = SearchService(Config(), db)
    path = tmp_path / "videos.csv"
    path.write_text('url;description;published_at\nhttps://www.tiktok.com/@fixture/video/111;#cs2;2026-05-01\nhttps://www.tiktok.com/@fixture/video/112;#nft;2026-04-30\n', encoding="utf-8-sig")
    assert service.import_file(path)["added"] == 2
    assert [row["video_id"] for row in service.search()] == ["111"]
    task = db.ensure_task(Config().account_id, "111")
    db.update_task(task["id"], comment="RubUP какой скин выбрать?")
    assert service.search() == []
    path = tmp_path / "videos.json"
    path.write_text(json.dumps({"videos": [{"url": "https://www.tiktok.com/@fixture/video/113", "description": "#dota2", "published_at": "2026-05-03"}]}))
    assert service.import_file(path)["added"] == 1
    assert [row["video_id"] for row in service.search()] == ["113"]
    service.close()
    db.close()


def test_real_mode_rejects_test_marker_and_mismatched_id(tmp_path):
    db = Database(tmp_path / "history.sqlite3")
    service = SearchService(Config(), db)
    result = service.add_url("https://www.tiktok.com/@fixture/video/111", is_test=True)
    assert not result.is_test
    with pytest.raises(ValueError, match="не совпадает"):
        service.add_url("https://www.tiktok.com/@fixture/video/112", video_id="999")
    service.close()
    db.close()


def test_oembed_enrichment_uses_fixed_endpoint_and_does_not_invent_date(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"title": "ролик #cs2", "author_name": "known_author"})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    db = Database(tmp_path / "history.sqlite3")
    service = SearchService(Config(), db, client)
    service.add_url("https://www.tiktok.com/@fixture/video/111")
    updated = service.enrich("111")
    assert updated.description == "ролик #cs2"
    assert updated.author == "known_author"
    assert updated.published_at is None
    assert analyze_video(updated, Config())["needs_review"]
    assert requests[0].url.host == "www.tiktok.com"
    assert requests[0].url.path == "/oembed"
    assert requests[0].url.params["url"] == updated.url
    client.close()
    db.close()


def test_tiktok_rate_limit_stops_without_retries(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    db = Database(tmp_path / "history.sqlite3")
    service = SearchService(Config(), db, client)
    service.add_url("https://www.tiktok.com/@fixture/video/111")
    with pytest.raises(DiscoveryError, match="ограничил"):
        service.enrich("111")
    assert len(calls) == 1
    client.close()
    db.close()


def test_dry_run_never_requests_oembed(tmp_path):
    def handler(request):
        raise AssertionError("Unexpected real request")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    db = Database(tmp_path / "dry.sqlite3")
    service = SearchService(Config(dry_run=True), db, client)
    service.add_url("https://www.tiktok.com/@fixture/video/111", is_test=True)
    with pytest.raises(DiscoveryError, match="dry-run"):
        service.enrich("111")
    client.close()
    db.close()


def test_small_cycles_do_not_starve_later_unknown_date_imports(tmp_path):
    db = Database(tmp_path / "history.sqlite3")
    service = SearchService(Config(max_videos=1), db)
    service.import_urls("https://www.tiktok.com/@fixture/video/111\nhttps://www.tiktok.com/@fixture/video/112")
    first = service.search()[0]
    db.ensure_task(service.config.account_id, first["video_id"])
    second = service.search()[0]
    assert second["video_id"] != first["video_id"]
    service.close()
    db.close()


def test_oversized_import_is_rejected_before_database_changes(tmp_path):
    db = Database(tmp_path / "history.sqlite3")
    service = SearchService(Config(), db)
    urls = "\n".join(f"https://www.tiktok.com/@fixture/video/{i}" for i in range(10001))
    with pytest.raises(ValueError, match="10 000"):
        service.import_urls(urls)
    assert db.list_videos() == []
    service.close()
    db.close()
