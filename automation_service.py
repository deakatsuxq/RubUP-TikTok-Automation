"""RubUP 4 workflow: user's comment bank and manual browser confirmations.

This runtime does not construct a text generator, read an AI credential, call an
AI provider, or infer that filling a field published a comment. Browser actions
are limited to navigation and filling; publication and own-like evidence comes
from explicit user confirmation.
"""
from __future__ import annotations

from dataclasses import replace
import logging
from pathlib import Path
import re
import threading
import time
import uuid

import httpx

from automation_store import AutomationStore
from comment_bank import CommentBank
from config import Config, load_config
from credentials import CredentialStore
from database import Database, _now
from link_resolver import LinkResolver
from telegram_bot import TelegramBot
from validators import ValidationError, canonical_tiktok_url


class _Cancelled:
    def __init__(self, *events):
        self.events = tuple(event for event in events if event is not None)

    def is_set(self):
        return any(event.is_set() for event in self.events)

    def wait(self, timeout=None):
        """Match threading.Event's polling protocol across every cancel source."""
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        while not self.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            delay = .05 if remaining is None else min(.05, remaining)
            if self.events:
                self.events[0].wait(delay)
            else:
                time.sleep(delay)
        return True


class _DemoBrowser:
    """No browser or network is opened by the application's test mode."""
    def __init__(self, root):
        self.root = Path(root)

    def profile_path(self, slot):
        if type(slot) is not int or slot not in (1, 2):
            raise ValidationError('Выберите аккаунт 1 или 2')
        return self.root / 'data' / 'playwright_profiles' / f'account-{slot}'

    def open_and_fill(self, slot, url, text, stop_event=None):
        self.profile_path(slot)
        canonical_tiktok_url(url)
        return {'slot': slot, 'filled': not (stop_event and stop_event.is_set()),
                'message': 'Тестовый режим: поле имитируется, браузер и TikTok не открываются.'}

    def close(self):
        pass


class _TelegramOutbox:
    """Share the durable URL ledger without updating historical v2/v3 tasks."""
    def __init__(self, db):
        self.db = db

    def reserve_telegram(self, url):
        return self.db.reserve_telegram(url)

    def finish_telegram(self, url, status, error=''):
        if status not in {'SENT', 'FAILED', 'UNKNOWN'}:
            raise ValueError('Неизвестный статус отправки Telegram')
        with self.db._connection(write=True) as conn:
            row = conn.execute('SELECT status FROM telegram_outbox WHERE url=?', (url,)).fetchone()
            if row is None:
                raise KeyError('Ссылка не была зарезервирована перед отправкой')
            if row['status'] == 'SENT' and status != 'SENT':
                raise ValueError('Успешная отправка уже сохранена')
            if row['status'] == 'UNKNOWN' and status not in {'UNKNOWN', 'SENT'}:
                raise ValueError('Результат требует проверки пользователем')
            conn.execute('UPDATE telegram_outbox SET status=?,error=?,updated_at=? WHERE url=?',
                         (status, str(error), _now(), url))


class AutomationService:
    """Durable two-account queues; every public browser operation is explicit."""

    MAX_LINKS = 100

    def __init__(self, config_path='config.yaml', *, config=None, browser=None,
                 client=None, test_mode=False, root=None):
        config_candidate = Path(config_path)
        if root is not None and not config_candidate.is_absolute():
            config_candidate = Path(root).resolve() / config_candidate
        self.config_path = config_candidate.resolve()
        source_root = Path(root).resolve() if root is not None else self.config_path.parent
        self.source_root = source_root
        if config is None:
            if self.config_path.is_file():
                config = load_config(self.config_path)
            else:
                config = Config(db_path=str(source_root / 'data' / 'history.sqlite3'))
                config._base_dir = source_root
        self.config = config
        self.test_mode = bool(test_mode or config.dry_run)
        self.is_test = self.test_mode
        self.root = source_root
        if self.test_mode:
            # Exclusive creation prevents a pre-existing database from ever
            # becoming demo data, even when its filename resembles a demo.
            sessions = source_root / 'data' / 'v4_test_sessions'
            sessions.mkdir(parents=True, exist_ok=True)
            self.root = sessions / ('session-' + uuid.uuid4().hex)
            self.root.mkdir(mode=0o700, exist_ok=False)
            db_path = self.root / 'data' / 'history.sqlite3'
        else:
            db_path = config.resolve_path(config.db_path)
        self.db = Database(db_path)
        try:
            self.store = AutomationStore(self.db)
            self.bank = CommentBank(self.store)
        except Exception:
            self.db.close()
            raise
        self.credential_store = CredentialStore(self.root)
        self._owns_client = client is None or self.test_mode
        if self.test_mode:
            def deny_network(request):
                raise httpx.RequestError('Сеть отключена в тестовом режиме', request=request)
            self._client = httpx.Client(transport=httpx.MockTransport(deny_network), timeout=config.timeout)
        else:
            self._client = client or httpx.Client(timeout=config.timeout, follow_redirects=False)
        self.resolver = LinkResolver(self._client, timeout=config.timeout)
        self._browser = _DemoBrowser(self.root) if self.test_mode else browser
        self._closed = False
        self._cancel = threading.Event()
        self._operation_lock = threading.RLock()
        self._telegram_lock = threading.RLock()
        self.log = logging.getLogger('rubup')
        # A crash never means an implicit new run. Keep the queue and require
        # Start after restart; PAUSED and STOPPED remain exactly as saved.
        if self.store.get_setting('run_state', 'IDLE') == 'RUNNING':
            self.store.set_setting('run_state', 'PAUSED')
        if self.state() in {'PAUSED', 'STOPPED'}:
            self._cancel.set()

    @property
    def browser(self):
        if self._browser is None:
            from playwright_browser import PlaywrightBrowser
            timeout_ms = min(120000, max(100, int(self.config.timeout * 1000)))
            self._browser = PlaywrightBrowser(self.root, timeout_ms=timeout_ms)
        return self._browser

    def profiles(self):
        return self.store.profiles()

    def tasks(self, profile_id=None, pending_only=False):
        return self.store.tasks(profile_id=profile_id, pending_only=pending_only)

    def stats(self):
        return self.store.stats()

    def state(self):
        return self.store.get_setting('run_state', 'IDLE')

    def resume(self):
        if self._closed:
            raise ValidationError('Программа уже закрывается')
        self._cancel.clear()
        self.store.set_setting('run_state', 'RUNNING')
        self.log.info('Пользователь запустил очередь RubUP 4')
        return self.state()

    def pause(self):
        self._cancel.set()
        self.store.set_setting('run_state', 'PAUSED')
        self.log.info('Очередь приостановлена пользователем')
        return self.state()

    def stop(self):
        self._cancel.set()
        self.store.set_setting('run_state', 'STOPPED')
        self.log.info('Очередь остановлена пользователем; прогресс сохранён')
        return self.state()

    def import_links(self, text, stop_event=None):
        if not isinstance(text, str):
            raise ValidationError('Вставьте ссылки, каждую с новой строки')
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) > self.MAX_LINKS:
            raise ValidationError(f'За один раз можно добавить не больше {self.MAX_LINKS} ссылок')
        cancellation = _Cancelled(self._cancel, stop_event)
        links, errors = [], []
        with self._operation_lock:
            for index, url in enumerate(lines, 1):
                if cancellation.is_set():
                    break
                try:
                    if self.test_mode:
                        canonical, video_id = canonical_tiktok_url(url)
                    else:
                        canonical, video_id = self.resolver.resolve(url)
                    links.append((canonical, video_id))
                except ValueError:
                    # Never echo input or HTTP exception text: a pasted URL
                    # may contain credentials or unrelated private query data.
                    errors.append(f'Строка {index}: нужна прямая ссылка TikTok; если короткая не раскрылась, скопируйте полный адрес видео.')
            imported = self.store.add_videos(links)
        result = {**imported, 'rejected': len(errors), 'errors': errors,
                  'cancelled': cancellation.is_set()}
        self.log.info('Импорт RubUP 4: добавлено %s видео, отклонено %s строк',
                      result.get('added', 0), len(errors))
        return result

    def prepare(self):
        if self._cancel.is_set():
            return {'assigned': 0, 'missing': sum(not task.get('comment') for task in self.tasks()),
                    'available': self.bank.stats()['available'], 'cancelled': True}
        with self._operation_lock:
            result = self.store.assign_comments()
        self.log.info('Назначено комментариев из банка: %s; недостаёт: %s',
                      result.get('assigned', 0), result.get('missing', 0))
        return result

    def open_login(self, slot):
        if type(slot) is not int or slot not in (1, 2):
            raise ValidationError('Выберите аккаунт 1 или 2')
        if self.test_mode:
            return {'slot': slot, 'message': 'Тестовый режим: вход в TikTok отключён, рабочие сессии не открываются.'}
        return self.browser.open_login(slot)

    @staticmethod
    def _safe_browser_error(exc):
        if isinstance(exc, ValidationError):
            return str(exc)
        # BrowserError deliberately contains safe, bounded messages; unknown
        # integration exceptions can contain browser/session URLs and tokens.
        if type(exc).__name__ == 'BrowserError' and type(exc).__module__ == 'playwright_browser':
            return str(exc)
        return 'Не удалось открыть или заполнить поле. Проверьте браузер и повторите действие.'

    def open_task(self, task_id, stop_event=None):
        task = self.store.get_task(task_id)
        if task.get('published') or task.get('status') == 'DONE':
            return {**task, 'filled': False, 'cached': True,
                    'message': 'Публикация уже подтверждена. Повторное заполнение заблокировано; при необходимости подтвердите собственный лайк.'}
        if not task.get('comment'):
            raise ValidationError('Для задания не хватает комментария. Добавьте комментарии в банк и нажмите «Старт».')
        cancellation = _Cancelled(self._cancel, stop_event)
        if cancellation.is_set():
            return {**task, 'filled': False, 'cancelled': True,
                    'message': 'Обработка приостановлена. Прогресс сохранён.'}
        with self._operation_lock:
            # A concurrent confirmation must never be followed by another fill.
            task = self.store.get_task(task_id)
            if task.get('published'):
                return {**task, 'filled': False, 'cached': True}
            try:
                result = self.browser.open_and_fill(task['slot'], task['url'], task['comment'],
                                                    stop_event=cancellation)
            except Exception as exc:
                if cancellation.is_set():
                    return {**self.store.get_task(task_id), 'filled': False, 'cancelled': True}
                error = self._safe_browser_error(exc)
                self.store.mark_error(task_id, error)
                self.log.warning('Поле не заполнено: задание %s', task_id)
                raise ValidationError(error) from None
            if cancellation.is_set():
                return {**self.store.get_task(task_id), 'filled': False, 'cancelled': True,
                        'message': 'Обработка приостановлена. Публикация не подтверждена.'}
            if result.get('filled') is True:
                self.store.mark_filled(task_id)
                self.log.info('Поле заполнено пользователю: задание %s; публикация не выполнялась', task_id)
            else:
                self.store.mark_error(task_id, 'Поле комментария не заполнено. Проверьте вход, доступность ролика и поле в браузере.')
            return {**self.store.get_task(task_id), **result, 'filled': result.get('filled') is True}

    def confirm_publication(self, task_id):
        with self._operation_lock:
            self.store.confirm_publication(task_id)
            task = self.store.get_task(task_id)
        self.log.info('Пользователь подтвердил публикацию: задание %s', task_id)
        return task

    def confirm_manual_fill(self, task_id):
        """Record the user's explicit fallback when TikTok changed its fields.

        The GUI asks the user to verify that the exact assigned text was pasted
        in the selected account. This is neither automatic field detection nor
        publication evidence; publication still needs its separate confirmation.
        """
        with self._operation_lock:
            task = self.store.get_task(task_id)
            if task.get('published'):
                raise ValidationError('Публикация уже подтверждена; повторная вставка не требуется')
            if not task.get('comment'):
                raise ValidationError('Сначала назначьте комментарий из банка')
            self.store.mark_filled(task_id)
            with self.db._connection(write=True) as conn:
                self.store._event(conn, 'MANUAL_FIELD_FILL_CONFIRMED', task_id)
            task = self.store.get_task(task_id)
        self.log.info('Пользователь подтвердил ручную вставку текста: задание %s', task_id)
        return task

    def confirm_like(self, task_id):
        with self._operation_lock:
            self.store.confirm_like(task_id)
            task = self.store.get_task(task_id)
        self.log.info('Пользователь подтвердил собственный лайк: задание %s', task_id)
        return task

    def telegram_settings(self):
        return {'enabled': bool(self.store.get_setting('telegram_enabled', False)),
                'token': self.credential_store.get('TELEGRAM_BOT_TOKEN'),
                'chat_id': self.credential_store.get('TELEGRAM_CHAT_ID')}

    def save_telegram(self, token, chat_id, enabled):
        if not isinstance(token, str) or not isinstance(chat_id, str):
            raise ValidationError('Токен и Chat ID должны быть строками')
        token, chat_id = token.strip(), chat_id.strip()
        if enabled and (not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token) or not chat_id):
            raise ValidationError('Для отправки укажите корректный токен Telegram-бота и Chat ID')
        self.credential_store.update({'TELEGRAM_BOT_TOKEN': token, 'TELEGRAM_CHAT_ID': chat_id})
        self.store.set_setting('telegram_enabled', bool(enabled))
        self.log.info('Настройки Telegram сохранены локально')
        return {'enabled': bool(enabled)}

    def drain_telegram(self):
        result = {'sent': 0, 'pending': 0, 'unknown': 0, 'errors': []}
        if self._closed:
            result['errors'].append('Программа уже закрывается; отправка остановлена.')
            return result
        with self._telegram_lock:
            eligible = self.store.eligible_videos()
            settings = self.telegram_settings()
            if self.test_mode or not settings['enabled']:
                result['pending'] = sum(self.db.telegram_status(row['url']) not in {'SENT', 'UNKNOWN', 'RESERVED', 'IN_FLIGHT', 'SENDING'} for row in eligible)
                result['unknown'] = sum(self.db.telegram_status(row['url']) in {'UNKNOWN', 'RESERVED', 'IN_FLIGHT', 'SENDING'} for row in eligible)
                return result
            bot = TelegramBot(replace(self.config, telegram_mode='published', dry_run=False),
                              _TelegramOutbox(self.db), client=self._client,
                              token=settings['token'], chat_id=settings['chat_id'])
            try:
                for row in eligible:
                    # A timeout or process crash is never permission to resend.
                    before = self.db.telegram_status(row['url'])
                    if before == 'SENT':
                        continue
                    if before in {'UNKNOWN', 'RESERVED', 'IN_FLIGHT', 'SENDING'}:
                        result['unknown'] += 1
                        continue
                    if self._cancel.is_set() or self._closed:
                        result['pending'] += 1
                        continue
                    task = {**row, 'id': None, 'is_test': False, 'publication_status': 'PUBLISHED'}
                    try:
                        if bot.send_video(task):
                            result['sent'] += 1
                            self.log.info('Telegram подтвердил отправку ссылки на видео %s', row['video_id'])
                    except Exception as exc:
                        from api_client import IntegrationError
                        safe = str(exc) if isinstance(exc, (IntegrationError, ValidationError)) else 'Не удалось отправить ссылку. Проверьте настройки и соединение.'
                        result['errors'].append(safe)
                        self.log.warning('Отправка Telegram не подтверждена для видео %s', row['video_id'])
                    after = self.db.telegram_status(row['url'])
                    if after in {'UNKNOWN', 'RESERVED', 'IN_FLIGHT', 'SENDING'}:
                        result['unknown'] += 1
                    elif after != 'SENT':
                        result['pending'] += 1
            finally:
                bot.close()
        return result

    def demo(self):
        """Prepare fifty sample videos without touching the working queue."""
        if not self.test_mode:
            isolated = AutomationService(self.config_path, config=replace(self.config, dry_run=False),
                                         test_mode=True, root=self.source_root)
            try:
                return isolated.demo()
            finally:
                isolated.close()
        self.resume()
        self.bank.import_text('\n'.join(f'RubUP — тестовый комментарий {index:03d}.' for index in range(1, 101)))
        self.import_links('\n'.join(f'https://www.tiktok.com/@rubup_demo/video/{8000000000000000000 + index}' for index in range(50)))
        self.prepare()
        return {**self.stats(), 'is_test': True, 'api_requests': 0,
                'session_path': str(self.root), 'message': 'Демонстрация: 50 видео, 100 комментариев. Публикации, лайки и Telegram не выполнялись.'}

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._cancel.set()
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            self.resolver.close()
            if self._owns_client:
                self._client.close()
            self.db.close()
