"""Two persistent Playwright profiles; navigation and comment filling only.

All Playwright objects belong to a single background thread.  This adapter never
presses Enter or clicks a publish/like button.  TikTok login and challenges remain
manual, in the visible browser opened for the selected account.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

from validators import canonical_tiktok_url


class BrowserError(ValueError):
    """Safe, user-facing browser error without URLs, session data or exceptions."""


_COMMENT_SELECTORS = (
    '[data-e2e="comment-input"][contenteditable="true"]',
    '[data-e2e="comment-input"] [contenteditable="true"]',
    '[data-e2e="comment-text"][contenteditable="true"]',
    '[data-e2e="comment-text"] [contenteditable="true"]',
    '[data-e2e="comment-input"] textarea',
    'textarea[data-e2e="comment-input"]',
    'textarea[placeholder*="comment" i]',
    'textarea[placeholder*="комментар" i]',
    'textarea[aria-label*="comment" i]',
    'textarea[aria-label*="комментар" i]',
    '[contenteditable="true"][data-placeholder*="comment" i]',
    '[contenteditable="true"][data-placeholder*="комментар" i]',
    '[contenteditable="true"][aria-label*="comment" i]',
    '[contenteditable="true"][aria-label*="комментар" i]',
)
_COMMENT_LOCATOR = ','.join(_COMMENT_SELECTORS)
_MANUAL = (
    'Поле комментария недоступно. Войдите в выбранный TikTok-аккаунт, '
    'пройдите показанную проверку вручную и откройте комментарии к ролику. '
    'Затем нажмите «Открыть и подставить» ещё раз. '
    'Если интерфейс TikTok изменился, скопируйте текст и вставьте его вручную.'
)


class PlaywrightBrowser:
    def __init__(self, root, *, headless=False, test_mode=False, timeout_ms=15000, executable_path=None):
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, (int, float)) or not 100 <= timeout_ms <= 120000:
            raise BrowserError('Тайм-аут браузера должен быть от 100 до 120000 мс')
        self.root = Path(root).resolve()
        self.headless = bool(headless)
        self.test_mode = bool(test_mode)
        self.timeout_ms = int(timeout_ms)
        # Explicit override supports integration tests on hosts with a system
        # Chromium. Windows uses Playwright's installed Chromium by default.
        self.executable_path = str(Path(executable_path).resolve()) if executable_path else None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='rubup-playwright')
        self._lock = threading.Lock()
        self._closed = False
        self._playwright = None
        self._contexts = {}
        self._pages = {}

    @staticmethod
    def _slot(slot):
        if type(slot) is not int or slot not in (1, 2):
            raise BrowserError('Выберите аккаунт 1 или 2')
        return slot

    def profile_path(self, slot):
        slot = self._slot(slot)
        folder = 'playwright_test_profiles' if self.test_mode else 'playwright_profiles'
        return self.root / 'data' / folder / f'account-{slot}'

    def _call(self, function, *args):
        with self._lock:
            if self._closed:
                raise BrowserError('Браузер закрыт. Перезапустите программу.')
            future = self._executor.submit(function, *args)
        try:
            return future.result()
        except BrowserError:
            raise
        except Exception:
            # Playwright errors can contain URLs, cookies and page excerpts.
            raise BrowserError('Не удалось выполнить действие в браузере. Проверьте подключение и повторите открытие.') from None

    def _context(self, slot):
        context = self._contexts.get(slot)
        if context is not None:
            try:
                # A manually closed persistent Chromium context cannot be reused.
                context.pages
                if context.browser and context.browser.is_connected():
                    return context
            except Exception:
                pass
            self._contexts.pop(slot, None)
            self._pages.pop(slot, None)
        if self._playwright is None:
            try:
                from playwright.sync_api import sync_playwright
                self._playwright = sync_playwright().start()
            except Exception:
                raise BrowserError('Playwright не установлен. Запустите install.bat и дождитесь установки Chromium.') from None
        directory = self.profile_path(slot)
        directory.mkdir(parents=True, exist_ok=True)
        try:
            directory.chmod(0o700)
            context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(directory), headless=self.headless,
                viewport={'width': 1280, 'height': 850},
                timeout=self.timeout_ms, accept_downloads=False,
                **({'executable_path': self.executable_path} if self.executable_path else {}),
            )
            context.set_default_timeout(self.timeout_ms)
            context.set_default_navigation_timeout(self.timeout_ms)
            if self.test_mode:
                # Even a fixture containing an external script/image cannot send
                # a real request. Test profiles never visit TikTok or Telegram.
                def local_only(route):
                    if self._local_url(route.request.url):
                        route.continue_()
                    else:
                        route.abort()
                context.route('**/*', local_only)
        except Exception:
            raise BrowserError(
                'Не удалось открыть Chromium. Запустите install.bat для установки браузера; '
                'закройте другое окно RubUP, использующее тот же профиль. '
                'Проверьте права записи в папку программы.'
            ) from None
        self._contexts[slot] = context
        return context

    def _page(self, slot):
        context = self._context(slot)
        page = self._pages.get(slot)
        if page is None or page.is_closed():
            page = context.pages[0] if context.pages else context.new_page()
            self._pages[slot] = page
        return page

    @staticmethod
    def _local_url(url):
        try:
            value = urlsplit(url)
            return (value.scheme in ('http', 'https')
                    and value.hostname in ('localhost', '127.0.0.1', '::1')
                    and value.username is None and value.password is None)
        except (TypeError, ValueError):
            return False

    def _validate_video_url(self, url):
        if self.test_mode:
            if not self._local_url(url):
                raise BrowserError('Тестовый браузер открывает только локальные тестовые страницы')
            return url
        try:
            return canonical_tiktok_url(url)[0]
        except (TypeError, ValueError):
            raise BrowserError('Нужна прямая HTTPS-ссылка на видео TikTok') from None

    def _allowed_page(self, url):
        if self.test_mode:
            return self._local_url(url)
        try:
            parsed = urlsplit(url)
            return (parsed.scheme == 'https' and parsed.hostname in ('www.tiktok.com', 'tiktok.com')
                    and parsed.username is None and parsed.password is None and parsed.port is None)
        except (TypeError, ValueError):
            return False

    def _matches_video(self, actual_url, requested_url):
        if not self._allowed_page(actual_url):
            return False
        if self.test_mode:
            return True
        try:
            # TikTok may change an author's handle or append tracking params;
            # the numeric video identity must still match the task exactly.
            return canonical_tiktok_url(actual_url)[1] == canonical_tiktok_url(requested_url)[1]
        except (TypeError, ValueError):
            return False

    def open_login(self, slot):
        slot = self._slot(slot)
        if self.test_mode:
            raise BrowserError('В тестовом режиме вход в настоящий TikTok отключён')
        return self._call(self._open_login, slot)

    def _open_login(self, slot):
        page = self._page(slot)
        try:
            page.goto('https://www.tiktok.com/login', wait_until='domcontentloaded', timeout=self.timeout_ms)
            page.bring_to_front()
        except Exception:
            raise BrowserError('Не удалось открыть страницу входа TikTok. Проверьте сеть и повторите.') from None
        return {'slot': slot, 'message': (
            f'Открыт отдельный профиль аккаунта {slot}. Войдите самостоятельно через официальный TikTok. '
            'Программа не запрашивает пароль и не подтверждает авторизацию автоматически.'
        )}

    def open_and_fill(self, slot, url, text, stop_event=None):
        slot = self._slot(slot)
        url = self._validate_video_url(url)
        if not isinstance(text, str) or not text.strip() or len(text) > 1000:
            raise BrowserError('Нужен непустой комментарий длиной до 1000 символов')
        return self._call(self._open_and_fill, slot, url, text, stop_event)

    @staticmethod
    def _cancelled(stop_event):
        return stop_event is not None and stop_event.is_set()

    @staticmethod
    def _comparable_text(text):
        # Browsers use LF in textarea values and may render ordinary spaces as
        # NBSP in contenteditable. Preserve every other character and spacing.
        return text.replace('\r\n', '\n').replace('\r', '\n').replace('\u00a0', ' ')

    def _open_and_fill(self, slot, url, text, stop_event):
        cancelled = {'slot': slot, 'filled': False, 'cancelled': True, 'message': 'Открытие остановлено; публикация не подтверждена.'}
        if self._cancelled(stop_event):
            return cancelled
        page = self._page(slot)
        if self._cancelled(stop_event):
            return cancelled
        try:
            # Same-page retries preserve a manually completed login, CAPTCHA or
            # opened comment pane. Retrying never reloads the video needlessly.
            if page.url != url:
                page.goto(url, wait_until='domcontentloaded', timeout=self.timeout_ms)
            page.bring_to_front()
        except Exception:
            if self._cancelled(stop_event):
                return cancelled
            raise BrowserError('Не удалось открыть видео. Проверьте сеть, доступность ролика и повторите.') from None
        if self._cancelled(stop_event):
            return cancelled
        if not self._matches_video(page.url, url):
            raise BrowserError('Открыта другая страница вместо нужного видео. Вставка остановлена. ' + _MANUAL)
        deadline = time.monotonic() + self.timeout_ms / 1000
        while time.monotonic() < deadline:
            if self._cancelled(stop_event):
                return cancelled
            try:
                # A selector that looks like a wrapper must never receive keys.
                # Only a visible, editable comment-specific element is filled.
                candidates = page.locator(_COMMENT_LOCATOR)
                for index in range(min(candidates.count(), 20)):
                    if self._cancelled(stop_event):
                        return cancelled
                    field = candidates.nth(index)
                    if field.is_visible() and field.is_editable():
                        if not self._matches_video(page.url, url):
                            raise BrowserError('Открыто другое видео; вставка остановлена.')
                        field.fill(text, timeout=min(self.timeout_ms, 3000))
                        if self._cancelled(stop_event):
                            return cancelled
                        actual = field.evaluate('(element) => "value" in element ? element.value : element.innerText')
                        if not self._matches_video(page.url, url):
                            raise BrowserError('После вставки открылась другая страница. Публикация не подтверждена.')
                        if not isinstance(actual, str) or self._comparable_text(actual) != self._comparable_text(text):
                            raise BrowserError(
                                'Страница изменила или сократила комментарий. Подстановка не подтверждена. '
                                'Проверьте ограничение длины и полный текст в TikTok вручную перед продолжением.'
                            )
                        return {'slot': slot, 'filled': True, 'message': (
                            'Комментарий подставлен. Проверьте аккаунт и текст, '
                            'опубликуйте его вручную в TikTok, затем подтвердите публикацию в программе. '
                            'Лайк своему комментарию также поставьте вручную.'
                        )}
            except BrowserError:
                raise
            except Exception:
                # Re-rendered or detached comment inputs can be retried locally.
                pass
            if stop_event is not None:
                stop_event.wait(0.1)
            else:
                time.sleep(0.1)
        raise BrowserError(_MANUAL)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            future = self._executor.submit(self._close)
        try:
            future.result()
        except Exception:
            pass
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)

    def _close(self):
        for context in self._contexts.values():
            try:
                context.close()
            except Exception:
                pass
        self._contexts.clear()
        self._pages.clear()
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
