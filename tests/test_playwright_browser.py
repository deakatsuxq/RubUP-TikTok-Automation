"""Real Chromium checks use local fixture pages and never contact TikTok."""
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
import shutil

import pytest

from playwright_browser import BrowserError, PlaywrightBrowser


@pytest.fixture(scope='module')
def local_site():
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == '/redirect':
                self.send_response(302)
                self.send_header('Location', 'https://example.com/blocked')
                self.end_headers()
            elif self.path == '/slow':
                time.sleep(0.35)
                self.path = '/comments.html'
                super().do_GET()
            else:
                super().do_GET()

    directory = Path(__file__).parent / 'fixtures'
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(directory)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}'
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.fixture
def browser(tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    executable = None
    with playwright.sync_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).exists():
            executable = shutil.which('chromium') or shutil.which('chromium-browser')
            if not executable:
                pytest.skip('Playwright Chromium is not installed; run python -m playwright install chromium')
    instance = PlaywrightBrowser(tmp_path, headless=True, test_mode=True, timeout_ms=2000, executable_path=executable)
    yield instance
    instance.close()


def inspect(browser, slot, expression):
    return browser._call(lambda: browser._pages[slot].evaluate(expression))


def test_profiles_have_two_distinct_stable_paths(tmp_path):
    browser = PlaywrightBrowser(tmp_path)
    try:
        assert browser.profile_path(1) == tmp_path / 'data/playwright_profiles/account-1'
        assert browser.profile_path(2) == tmp_path / 'data/playwright_profiles/account-2'
        assert browser.profile_path(1) != browser.profile_path(2)
        assert not (tmp_path / 'data').exists()  # lazy, no browser needed
        for invalid in (0, 3, '1', True, None):
            with pytest.raises(BrowserError):
                browser.profile_path(invalid)
    finally:
        browser.close()


def test_test_profiles_do_not_share_production_paths(tmp_path):
    production = PlaywrightBrowser(tmp_path)
    test = PlaywrightBrowser(tmp_path, test_mode=True)
    try:
        assert test.profile_path(1) != production.profile_path(1)
        with pytest.raises(BrowserError, match='тестовом режиме'):
            test.open_login(1)
        with pytest.raises(BrowserError, match='локальные'):
            test.open_and_fill(1, 'https://www.tiktok.com/@test/video/123', 'RubUP local')
    finally:
        production.close()
        test.close()


def test_production_rejects_non_tiktok_before_launch(tmp_path):
    browser = PlaywrightBrowser(tmp_path)
    try:
        for url in ('http://127.0.0.1/', 'https://example.com/', 'file:///tmp/page.html', 'https://vt.tiktok.com/abc/'):
            with pytest.raises(BrowserError, match='HTTPS-ссылка'):
                browser.open_and_fill(1, url, 'RubUP local')
        assert not (tmp_path / 'data').exists()
    finally:
        browser.close()


def test_production_checks_video_identity_including_after_redirect(tmp_path):
    browser = PlaywrightBrowser(tmp_path)
    expected = 'https://www.tiktok.com/@first/video/123'
    try:
        assert browser._matches_video('https://www.tiktok.com/@renamed/video/123?source=web', expected)
        assert not browser._matches_video('https://www.tiktok.com/@first/video/456', expected)
        assert not browser._matches_video('https://www.tiktok.com/login', expected)
        assert not browser._matches_video('https://example.com/@first/video/123', expected)
    finally:
        browser.close()


@pytest.mark.parametrize('text', ['', '   ', None, 'x' * 1001])
def test_invalid_text_never_opens_browser(tmp_path, text):
    browser = PlaywrightBrowser(tmp_path, test_mode=True)
    try:
        with pytest.raises(BrowserError, match='комментарий'):
            browser.open_and_fill(1, 'http://localhost/', text)
        assert not (tmp_path / 'data').exists()
    finally:
        browser.close()


def test_cancel_before_launch_never_creates_profile(tmp_path):
    browser = PlaywrightBrowser(tmp_path, test_mode=True)
    stop = threading.Event()
    stop.set()
    try:
        result = browser.open_and_fill(1, 'http://localhost/', 'RubUP local', stop)
        assert result['cancelled'] and not result['filled']
        assert not (tmp_path / 'data').exists()
    finally:
        browser.close()


def test_textarea_filled_without_post_or_like(browser, local_site):
    result = browser.open_and_fill(1, local_site + '/comments.html', 'Мой RubUP комментарий')
    assert result['filled'] is True
    state = inspect(browser, 1, '({text:document.querySelector("#comment").value, search:document.querySelector("#search").value, posts:window.posts, likes:window.likes})')
    assert state == {'text': 'Мой RubUP комментарий', 'search': '', 'posts': 0, 'likes': 0}


def test_contenteditable_filled_without_post_or_like(browser, local_site):
    result = browser.open_and_fill(2, local_site + '/contenteditable.html', 'RubUP — свой текст')
    assert result['filled']
    state = inspect(browser, 2, '({text:document.querySelector("#comment").textContent, posts:window.posts, likes:window.likes})')
    assert state == {'text': 'RubUP — свой текст', 'posts': 0, 'likes': 0}


@pytest.mark.parametrize('fixture,selector,value_expression', [
    ('comments.html', '#comment', 'element.value'),
    ('contenteditable.html', '#comment', 'element.textContent'),
])
def test_page_truncated_text_cannot_be_marked_filled(browser, local_site, fixture, selector, value_expression):
    url = local_site + '/' + fixture
    browser.open_and_fill(1, url, 'RubUP исходный текст')
    # TikTok can apply a length rule or rewrite a controlled input after fill.
    # This event handler represents that behavior without contacting TikTok.
    browser._call(lambda: browser._pages[1].locator(selector).evaluate(
        '(element) => element.addEventListener("input", () => {'
        + value_expression + ' = ' + value_expression + '.slice(0, 8); })'
    ))
    with pytest.raises(BrowserError, match='сократила'):
        browser.open_and_fill(1, url, 'RubUP мой полный комментарий')
    assert inspect(browser, 1, 'window.posts') == 0
    assert inspect(browser, 1, 'window.likes') == 0


def test_missing_field_keeps_login_manual_and_does_not_fill_other_field(browser, local_site):
    browser._call(browser._page, 1)
    browser.timeout_ms = 300
    with pytest.raises(BrowserError, match='вручную'):
        browser.open_and_fill(1, local_site + '/no_comment.html', 'RubUP — свой текст')
    assert inspect(browser, 1, 'document.querySelector("#password").value') == ''
    assert inspect(browser, 1, 'document.querySelector("#unrelated").value') == ''


def test_retry_preserves_manually_opened_comment_pane(browser, local_site):
    browser._call(browser._page, 1)
    browser.timeout_ms = 300
    with pytest.raises(BrowserError):
        browser.open_and_fill(1, local_site + '/no_comment.html', 'RubUP повтор')
    # This represents a user finishing login/verification or opening the pane.
    inspect(browser, 1, 'document.body.insertAdjacentHTML("beforeend", \'<textarea id="manual" placeholder="Add comment"></textarea>\')')
    result = browser.open_and_fill(1, local_site + '/no_comment.html', 'RubUP повтор')
    assert result['filled']
    assert inspect(browser, 1, 'document.querySelector("#manual").value') == 'RubUP повтор'


def test_profiles_have_isolated_persistent_storage(browser, local_site, tmp_path):
    browser.open_and_fill(1, local_site + '/comments.html', 'RubUP первый')
    inspect(browser, 1, 'localStorage.setItem("account", "one"); document.cookie="account=one; max-age=600; path=/"')
    browser.open_and_fill(2, local_site + '/comments.html', 'RubUP второй')
    assert inspect(browser, 2, 'localStorage.getItem("account")') is None
    assert 'account=one' not in inspect(browser, 2, 'document.cookie')
    inspect(browser, 2, 'localStorage.setItem("account", "two")')
    assert inspect(browser, 1, 'localStorage.getItem("account")') == 'one'
    browser.close()
    resumed = PlaywrightBrowser(tmp_path, headless=True, test_mode=True, timeout_ms=2000, executable_path=browser.executable_path)
    try:
        resumed.open_and_fill(1, local_site + '/comments.html', 'RubUP продолжение')
        resumed.open_and_fill(2, local_site + '/comments.html', 'RubUP второй продолжение')
        assert inspect(resumed, 1, 'localStorage.getItem("account")') == 'one'
        assert inspect(resumed, 2, 'localStorage.getItem("account")') == 'two'
        assert 'account=one' in inspect(resumed, 1, 'document.cookie')
    finally:
        resumed.close()


def test_parallel_callers_share_owner_thread_but_not_profiles(browser, local_site):
    with ThreadPoolExecutor(max_workers=2) as callers:
        first = callers.submit(browser.open_and_fill, 1, local_site + '/comments.html', 'RubUP один')
        second = callers.submit(browser.open_and_fill, 2, local_site + '/comments.html', 'RubUP два')
        assert first.result()['filled']
        assert second.result()['filled']
    assert inspect(browser, 1, 'document.querySelector("#comment").value') == 'RubUP один'
    assert inspect(browser, 2, 'document.querySelector("#comment").value') == 'RubUP два'


def test_cancel_during_navigation_does_not_fill(browser, local_site):
    stop = threading.Event()
    # Context creation is excluded so the signal arrives while navigation runs.
    browser._call(browser._page, 1)
    timer = threading.Timer(0.1, stop.set)
    timer.start()
    try:
        result = browser.open_and_fill(1, local_site + '/slow', 'RubUP отменённый', stop)
        assert result['cancelled'] and not result['filled']
        assert inspect(browser, 1, 'document.querySelector("#comment").value') == ''
    finally:
        timer.join(timeout=1)


def test_external_redirect_is_blocked_in_test_mode(browser, local_site):
    with pytest.raises(BrowserError):
        browser.open_and_fill(1, local_site + '/redirect', 'RubUP local')


def test_close_is_idempotent_and_rejects_new_operations(tmp_path):
    browser = PlaywrightBrowser(tmp_path, test_mode=True)
    browser.close()
    browser.close()
    with pytest.raises(BrowserError, match='закрыт'):
        browser.open_and_fill(1, 'http://localhost/', 'RubUP local')


def test_unknown_errors_are_sanitized(tmp_path):
    browser = PlaywrightBrowser(tmp_path, test_mode=True)
    try:
        def fail():
            raise RuntimeError('SECRET_SESSION_TOKEN and private URL')
        with pytest.raises(BrowserError) as caught:
            browser._call(fail)
        assert 'SECRET' not in str(caught.value)
    finally:
        browser.close()
