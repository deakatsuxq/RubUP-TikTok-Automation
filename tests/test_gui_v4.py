"""Exercise the 4.0 desktop with real SQLite and mocked external effects."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import threading
import time

import httpx
import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QMessageBox, QPlainTextEdit

from automation_service import AutomationService
from config import Config
from gui.automation_window import AutomationWindow, safe_message

_APP = None


class FakeBrowser:
    def __init__(self, root):
        self.root = root
        self.fills = []
        self.logins = []
        self.closed = False
        self.block = None

    def profile_path(self, slot):
        return self.root / 'data/playwright_profiles' / f'account-{slot}'

    def open_login(self, slot):
        self.logins.append((slot, threading.get_ident()))
        return {'slot': slot, 'message': 'Войдите на официальном сайте TikTok.'}

    def open_and_fill(self, slot, url, text, stop_event=None):
        self.fills.append((slot, url, text, threading.get_ident()))
        if self.block:
            self.block.set()
            stop_event.wait(5)
        return {'slot': slot, 'filled': not (stop_event and stop_event.is_set()), 'message': 'Текст подставлен, публикация вручную.'}

    def close(self):
        self.closed = True


@pytest.fixture(scope='module')
def qtapp():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    _APP.setQuitOnLastWindowClosed(False)
    return _APP


def wait(qtapp, predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qtapp.processEvents()
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError('Qt 4.0 operation did not finish')


@pytest.fixture
def service(tmp_path, monkeypatch):
    for key in ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'):
        monkeypatch.delenv(key, raising=False)
    client = httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail('Unexpected external request')))
    config = Config(db_path=str(tmp_path / 'data/history.sqlite3'))
    config._base_dir = tmp_path
    browser = FakeBrowser(tmp_path)
    instance = AutomationService(tmp_path / 'config.yaml', config=config, browser=browser, client=client)
    yield instance
    instance.close()
    client.close()


@pytest.fixture
def window(service, qtapp):
    instance = AutomationWindow(service)
    instance.show()
    qtapp.processEvents()
    yield instance
    instance.close()
    wait(qtapp, lambda: instance._allow_close and not instance.isVisible())
    instance.deleteLater()
    qtapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qtapp.processEvents()


def comments(count):
    return '\n'.join(f'Мой комментарий RubUP номер {index + 1}' for index in range(count))


def urls(count):
    return '\n'.join(f'https://www.tiktok.com/@fixture/video/{8800000000000000000 + index}' for index in range(count))


def start(window, service, qtapp, videos=1, texts=None):
    service.bank.import_text(comments(texts if texts is not None else videos * 2))
    window.links_input.setPlainText(urls(videos))
    window.start_button.click()
    wait(qtapp, lambda: window.worker is None and bool(service.browser.fills))


def yes(monkeypatch):
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.StandardButton.Yes)


def test_five_simple_pages_without_ai_or_cost(window):
    assert window.windowTitle() == 'RubUP Automation 4.0'
    assert [button.text() for button in window.nav_buttons] == ['Главная', 'Мои комментарии', 'Аккаунты', 'Telegram', 'Как пользоваться']
    assert window.pages.count() == 5
    assert isinstance(window.links_input, QPlainTextEdit)
    assert window.start_button.text() == 'СТАРТ'
    assert window.telegram_token.echoMode() == window.telegram_token.EchoMode.Password
    assert not window.publish_button.isEnabled()
    assert not window.like_button.isEnabled()
    assert not window.fill_button.isEnabled()
    texts = ' '.join(child.text() for child in window.findChildren(type(window.status_label)))
    assert 'OpenAI' not in texts and 'Стоимость' not in texts


def test_fifty_links_create_hundred_distinct_tasks_and_only_fill_first(window, service, qtapp):
    main_thread = threading.get_ident()
    start(window, service, qtapp, videos=50)
    assert len(service.tasks()) == 100
    assert len({task['comment_id'] for task in service.tasks()}) == 100
    assert len(service.browser.fills) == 1
    assert service.browser.fills[0][3] != main_thread
    assert service.stats()['published'] == 0
    assert service.stats()['completed'] == 0
    assert window.metrics['remaining'].text() == '100'
    assert window.metrics['used_comments'].text() == '100'
    assert window.publish_button.isEnabled()
    assert not window.like_button.isEnabled()
    assert 'Новых видео: 50' in window.import_summary.text()
    assert window.current_url.text().startswith('https://www.tiktok.com/')


def test_short_bank_warns_and_keeps_unassigned_tasks(window, service, qtapp):
    start(window, service, qtapp, videos=2, texts=1)
    assert len(service.tasks()) == 4
    assert service.stats()['used_comments'] == 1
    assert 'Не хватает комментариев: 3' in window.import_summary.text()
    assert sum(not task['comment'] for task in service.tasks()) == 3


def test_publication_and_like_need_separate_manual_confirmations(window, service, qtapp, monkeypatch):
    yes(monkeypatch)
    start(window, service, qtapp, videos=2)
    first_id = window.current_task['id']
    window.publish_button.click()
    assert service.store.get_task(first_id)['published']
    assert not service.store.get_task(first_id)['liked']
    assert window.like_button.isEnabled()
    assert len(service.browser.fills) == 1
    assert service.stats()['completed'] == 0
    window.like_button.click()
    wait(qtapp, lambda: window.worker is None and len(service.browser.fills) == 2)
    assert service.store.get_task(first_id)['status'] == 'DONE'
    assert service.stats()['completed'] == 1
    assert service.stats()['published'] == 1
    assert window.current_task['id'] != first_id
    assert len(service.tasks(profile_id=service.profiles()[1]['id'], pending_only=True)) == 2
    assert all(not task['published'] for task in service.tasks(profile_id=service.profiles()[1]['id']))


def test_declined_confirmation_does_not_mutate_publication(window, service, qtapp, monkeypatch):
    start(window, service, qtapp)
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.StandardButton.No)
    window.publish_button.click()
    assert service.stats()['published'] == 0
    assert not window.like_button.isEnabled()


def test_pause_cancels_fill_and_start_resumes_same_assignment(window, service, qtapp):
    service.browser.block = threading.Event()
    service.bank.import_text(comments(2))
    window.links_input.setPlainText(urls(1))
    window.start_button.click()
    wait(qtapp, lambda: service.browser.block.is_set())
    before = [task['comment_id'] for task in service.tasks()]
    assert not window.profile_choice.isEnabled()
    window.pause_button.click()
    wait(qtapp, lambda: window.worker is None)
    assert service.state() == 'PAUSED'
    assert service.stats()['published'] == 0
    assert not window.publish_button.isEnabled()
    service.browser.block = None
    window.start_button.click()
    wait(qtapp, lambda: window.worker is None and window.publish_button.isEnabled())
    assert [task['comment_id'] for task in service.tasks()] == before
    assert service.state() == 'RUNNING'


def test_bank_paste_and_reset_keep_pending_assignments(window, service, qtapp, monkeypatch):
    window.bank_input.setPlainText(comments(4) + '\n' + comments(1) + '\nнет упоминания бренда')
    window.bank_add_button.click()
    wait(qtapp, lambda: window.worker is None and service.bank.stats()['total'] == 4)
    assert 'Добавлено: 4' in window.bank_feedback.text()
    assert 'дубликатов: 1' in window.bank_feedback.text()
    assert 'отклонено: 1' in window.bank_feedback.text()
    window.links_input.setPlainText(urls(1))
    window.start_button.click()
    wait(qtapp, lambda: window.worker is None and bool(service.browser.fills))
    ids = [task['comment_id'] for task in service.tasks()]
    yes(monkeypatch)
    window.bank_reset_button.click()
    wait(qtapp, lambda: window.worker is None)
    assert [task['comment_id'] for task in service.tasks()] == ids
    assert service.bank.stats()['used'] == 2


def test_two_profile_login_is_background_and_names_persist(window, service, qtapp):
    window.account_controls[2]['name'].setText('@second-account')
    window.account_controls[2]['save'].click()
    assert service.profiles()[1]['name'] == '@second-account'
    window.account_controls[2]['login'].click()
    wait(qtapp, lambda: window.worker is None and bool(service.browser.logins))
    assert service.browser.logins[0][0] == 2
    assert service.browser.logins[0][1] != threading.get_ident()
    assert service.browser.profile_path(1) != service.browser.profile_path(2)
    window.select_profile_slot(2)
    assert window.profile_choice.currentData() == service.profiles()[1]['id']
    assert service.store.get_setting('gui_profile') == service.profiles()[1]['id']


def test_telegram_sent_once_after_both_accounts_complete(window, service, qtapp, monkeypatch):
    yes(monkeypatch)
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 4}})
    service._client.close()
    service._client = httpx.Client(transport=httpx.MockTransport(respond))
    window.telegram_token.setText('123456:secret_telegram_token')
    window.telegram_chat.setText('1234567')
    window.telegram_enabled.setChecked(True)
    window.telegram_save.click()
    wait(qtapp, lambda: window.worker is None)
    start(window, service, qtapp)
    window.publish_button.click()
    window.like_button.click()
    wait(qtapp, lambda: window.worker is None)
    assert requests == []
    window.select_profile_slot(2)
    window.fill_button.click()
    wait(qtapp, lambda: window.worker is None and window.publish_button.isEnabled())
    window.publish_button.click()
    window.like_button.click()
    wait(qtapp, lambda: window.worker is None and len(requests) == 1)
    import json
    assert json.loads(requests[0].content)['text'] == urls(1)
    window.telegram_retry.click()
    wait(qtapp, lambda: window.worker is None)
    assert len(requests) == 1
    assert service.stats()['telegram_sent'] == 1
    service._client.close()


def test_manual_fill_fallback_still_needs_publication_confirmation(window, service, qtapp, monkeypatch):
    service.bank.import_text(comments(2))
    service.resume()
    service.import_links(urls(1))
    service.prepare()
    window.reload()
    assert not window.publish_button.isEnabled()
    yes(monkeypatch)
    window.manual_fill_button.click()
    assert window.publish_button.isEnabled()
    assert service.stats()['published'] == 0
    window.publish_button.click()
    assert service.stats()['published'] == 1
    assert service.stats()['completed'] == 0
    assert service.browser.fills == []


def test_error_redaction_hides_bot_tokens():
    output = safe_message('https://api.telegram.org/bot123456:super_secret_token/sendMessage failed')
    assert 'super_secret_token' not in output


def test_stop_before_worker_begins_cannot_be_overwritten_by_late_resume(window, service, qtapp, monkeypatch):
    from gui.automation_window import AutomationWorker
    entered, release = threading.Event(), threading.Event()
    original = AutomationWorker.run
    def delayed(worker):
        entered.set()
        release.wait(5)
        original(worker)
    monkeypatch.setattr(AutomationWorker, 'run', delayed)
    service.bank.import_text(comments(2))
    window.links_input.setPlainText(urls(1))
    window.start_button.click()
    wait(qtapp, entered.is_set)
    window.stop_button.click()
    release.set()
    wait(qtapp, lambda: window.worker is None)
    assert service.state() == 'STOPPED'
    assert service.browser.fills == []
    assert service.tasks() == []


def test_demo_button_opens_fifty_videos_in_separate_service(window, service, qtapp):
    window.demo_button.click()
    wait(qtapp, lambda: window.worker is None and bool(window._demo_windows))
    demo = window._demo_windows[0]
    assert demo.test_mode and demo.mode_banner.isVisible()
    assert demo.service.root != service.root
    assert demo.service.stats()['total'] == 100
    assert demo.service.bank.stats()['total'] == 100
    assert demo.service.stats()['published'] == 0
    assert demo.service.stats()['telegram_sent'] == 0
    assert service.tasks() == []
    assert service.bank.stats()['total'] == 0
    assert service.browser.fills == []
    assert not demo.demo_button.isEnabled()
    assert all(not controls['login'].isEnabled() for controls in demo.account_controls.values())
    demo.close()
    wait(qtapp, lambda: not window._demo_windows)


def test_closing_during_demo_creation_closes_unshown_demo_service(window, service, qtapp, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    created = []
    original = AutomationService.demo
    def delayed(instance, *args, **kwargs):
        created.append(instance)
        entered.set()
        release.wait(5)
        return original(instance, *args, **kwargs)
    monkeypatch.setattr(AutomationService, 'demo', delayed)
    window.demo_button.click()
    wait(qtapp, entered.is_set)
    window.close()
    assert window._closing
    release.set()
    wait(qtapp, lambda: window._allow_close)
    assert created and created[0]._closed
    assert service._closed
    assert window._demo_windows == []
