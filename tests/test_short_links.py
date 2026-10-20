"""Redirect validation uses mocked HTTP only; no TikTok access or publications."""
import httpx
import pytest

from config import Config
from database import Database
from link_resolver import LinkResolver, SHORT_LINK_ERROR
from tiktok_search import SearchService
from validators import ValidationError, canonical_tiktok_url


DIRECT = "https://www.tiktok.com/@known_author/video/123456789"


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


@pytest.mark.parametrize("url", [DIRECT, "https://tiktok.com/@known_author/video/123456789?track=one#foo"])
def test_direct_links_normalize_without_http(url):
    with client_for(lambda request: pytest.fail("A direct link must not need HTTP")) as client:
        assert LinkResolver(client).resolve(url) == (DIRECT, "123456789")


@pytest.mark.parametrize("host", ["vt.tiktok.com", "vm.tiktok.com"])
def test_short_link_resolves_once_and_does_not_fetch_video(host):
    calls = []
    def route(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": DIRECT + "?tracking=discarded"})
    with client_for(route) as client:
        resolver = LinkResolver(client)
        assert resolver.resolve(f"https://{host}/ZSgA1H5dB/") == (DIRECT, "123456789")
    assert calls == [f"https://{host}/ZSgA1H5dB/"]


def test_relative_redirect_and_internal_web_alias_are_supported():
    calls = []
    def route(request):
        calls.append(str(request.url))
        if request.url.host == "vt.tiktok.com":
            return httpx.Response(301, headers={"Location": "https://www.tiktok.com/t/AnotherCode/"})
        return httpx.Response(307, headers={"Location": "/@known_author/video/123456789?x=1"})
    with client_for(route) as client:
        assert LinkResolver(client).resolve("https://vt.tiktok.com/ShortCode/")[0] == DIRECT
    assert len(calls) == 2


@pytest.mark.parametrize("url", [
    "https://evil.example/short", "https://vt.tiktok.com.evil.example/code/",
    "http://vt.tiktok.com/code/", "ftp://vt.tiktok.com/code/",
    "https://127.0.0.1/code/", "https://localhost/code/", "https://[::1]/code/",
    "https://user:password@vt.tiktok.com/code/", "https://vt.tiktok.com:443/code/",
    "https://vt.tiktok.com:/code/",
    "https://vt.tiktok.com:8080/code/", "https://vt.tiktok.com/%2e%2e/code/",
    "https://vt.tiktok.com/code\\evil", "https://vt.tiktok.com/code /",
    "https://www.tiktok.com/login", "https://vt.tiktok.com/",
    "https://vt.tiktok.com/code/extra", "https://www.tiktok.com/@a/video/%31%32%33",
])
def test_invalid_initial_url_is_rejected_before_network(url):
    with client_for(lambda request: pytest.fail("Invalid input reached HTTP")) as client:
        with pytest.raises(ValidationError):
            LinkResolver(client).resolve(url)


@pytest.mark.parametrize("location", [
    "https://evil.example/video", "//127.0.0.1/private", "https://localhost/private",
    "http://www.tiktok.com/@known_author/video/123456789",
    "https://user@www.tiktok.com/@known_author/video/123456789",
    "https://www.tiktok.com:443/@known_author/video/123456789",
    "https://www.tiktok.com:/@known_author/video/123456789",
    "https://www.tiktok.com.evil.example/@known_author/video/123456789",
    "https://www.tiktok.com/@known_author/video/%31%32%33",
    "https://vt.tiktok.com/other\\path", "https://www.tiktok.com/login",
    "https://www.tiktok.com/@known_author/video/123\n456789",
])
def test_unsafe_redirect_is_never_requested_and_shows_friendly_fallback(location):
    calls = []
    def route(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": location})
    with client_for(route) as client:
        with pytest.raises(ValidationError) as error:
            LinkResolver(client).resolve("https://vt.tiktok.com/ShortCode/")
    assert str(error.value) == SHORT_LINK_ERROR
    assert calls == ["https://vt.tiktok.com/ShortCode/"]


def test_redirect_count_is_bounded_even_when_client_follows_redirects_by_default():
    calls = []
    def route(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": f"https://vt.tiktok.com/hop{len(calls)}/"})
    with client_for(route) as client:
        with pytest.raises(ValidationError, match="Не удалось"):
            LinkResolver(client, max_redirects=2).resolve("https://vm.tiktok.com/start/")
    assert len(calls) == 3


def test_redirect_loop_stops_without_unbounded_requests():
    calls = []
    def route(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "/same/"})
    with client_for(route) as client:
        with pytest.raises(ValidationError, match="Не удалось"):
            LinkResolver(client).resolve("https://vt.tiktok.com/same/")
    assert len(calls) == 1


@pytest.mark.parametrize("status", [200, 403, 404, 429, 500])
def test_unavailable_link_or_antibot_page_is_not_treated_as_a_video(status):
    calls = []
    def route(request):
        calls.append(request)
        return httpx.Response(status, text="<html>verification required</html>")
    with client_for(route) as client:
        with pytest.raises(ValidationError) as error:
            LinkResolver(client).resolve("https://vt.tiktok.com/code/")
    assert str(error.value) == SHORT_LINK_ERROR
    assert len(calls) == 1


def test_network_timeout_is_sanitized_and_uses_a_timeout_budget():
    calls = []
    def route(request):
        calls.append(request)
        assert 0 < request.extensions["timeout"]["read"] <= 4
        raise httpx.ReadTimeout("internal network details must stay private", request=request)
    with client_for(route) as client:
        with pytest.raises(ValidationError) as error:
            LinkResolver(client, timeout=4).resolve("https://vt.tiktok.com/code/")
    assert str(error.value) == SHORT_LINK_ERROR
    assert "internal" not in str(error.value)
    assert len(calls) == 1


def test_import_deduplicates_short_links_by_actual_video_id(tmp_path):
    calls = []
    def route(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": DIRECT})
    db = Database(tmp_path / "history.sqlite3")
    with client_for(route) as client:
        search = SearchService(Config(), db, client)
        counts = search.import_urls("https://vt.tiktok.com/one/\nhttps://vm.tiktok.com/two/\n" + DIRECT)
    assert counts["added"] == 1 and counts["duplicate"] == 2
    assert len(calls) == 2
    assert db.list_videos()[0]["url"] == DIRECT
    db.close()


def test_dry_run_rejects_short_links_without_network(tmp_path):
    db = Database(tmp_path / "dry.sqlite3")
    with client_for(lambda request: pytest.fail("dry-run made an HTTP request")) as client:
        search = SearchService(Config(dry_run=True), db, client)
        result = search.import_urls("https://vt.tiktok.com/code/\n" + DIRECT)
    assert result["rejected"] == 1 and result["added"] == 1
    assert "демонстрационном" in result["errors"][0]
    db.close()


def test_direct_only_validator_remains_compatible():
    with pytest.raises(ValidationError):
        canonical_tiktok_url("https://vt.tiktok.com/code/")


def test_updated_search_settings_apply_timeout_without_restarting_client(tmp_path):
    timeouts = []
    def route(request):
        timeouts.append(request.extensions['timeout']['read'])
        return httpx.Response(302, headers={'Location': DIRECT})
    db = Database(tmp_path / 'history.sqlite3')
    with client_for(route) as client:
        search = SearchService(Config(timeout=7), db, client)
        search.resolve_url('https://vt.tiktok.com/one/')
        search.config = Config(timeout=2)
        search.resolve_url('https://vt.tiktok.com/two/')
    assert 2 < timeouts[0] <= 7
    assert 0 < timeouts[1] <= 2
    db.close()
