"""User workflows of the five-page interface, without live API calls."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import threading
import time

import httpx
import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QMessageBox, QLineEdit, QPlainTextEdit, QTableView

from application import AppService
from config import Config, save_config
from gui.main_window import MainWindow, safe_error
from models import Video

_QT_APP = None


@pytest.fixture(scope='module')
def qtapp():
    global _QT_APP
    _QT_APP = QApplication.instance() or QApplication([])
    _QT_APP.setQuitOnLastWindowClosed(False)
    return _QT_APP


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(httpx.Client, 'request', lambda *args, **kw: pytest.fail('Unexpected live network'))
    path = tmp_path / 'config.yaml'
    save_config(Config(db_path=str(tmp_path / 'history.sqlite3')), path)
    service = AppService(path)
    service.complete_onboarding()
    yield service
    service.close()


@pytest.fixture
def window(qtapp, service):
    obj = MainWindow(service)
    yield obj
    obj.close()
    for widget in QApplication.topLevelWidgets():
        if widget is not obj:
            widget.close()
            widget.deleteLater()
    obj.deleteLater()
    qtapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qtapp.processEvents()


def wait(qtapp, predicate, timeout=4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        qtapp.processEvents()
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError('Background UI job did not finish')


def task(service, published_at='2026-06-12'):
    service.db.add_video(Video('123456789', 'https://www.tiktok.com/@test/video/123456789',
                              description='#cs2 обзор скинов', hashtags=['cs2'], published_at=published_at))
    item = service.db.ensure_task(service.config.account_id, '123456789')
    service.save_comment(item['id'], 'реклама: какие скины в RubUP?', allow_preliminary=published_at is None)
    return service.task_view(item['id'])


def test_home_has_five_sections_three_metrics_and_no_table(window):
    assert [button.text() for button in window.nav_buttons] == ['Главная', 'Мои задания', 'Картинки', 'Настройки', 'Помощь']
    assert window.pages.count() == 5
    assert window.pages.currentIndex() == 0
    assert window.url_input.placeholderText() == 'Вставь ссылку на TikTok-видео'
    assert window.process_button.text() == 'НАЧАТЬ ОБРАБОТКУ'
    assert len(window.dashboard.METRICS) == 3
    assert not window.home_page.findChildren(QTableView)
    assert not window.result_card.isVisible()


def test_process_button_runs_pipeline_off_main_thread_and_shows_saved_result(window, service, qtapp, monkeypatch):
    item = task(service)
    calls = []
    main_thread = threading.get_ident()
    def process(url, stop_event, progress):
        calls.append((url, threading.get_ident()))
        progress('Готовим комментарий…')
        return item
    monkeypatch.setattr(service, 'process_url', process)
    window.url_input.setText(item['url'])
    window.process_button.click()
    wait(qtapp, lambda: window.worker is None)
    assert calls == [(item['url'], calls[0][1])]
    assert calls[0][1] != main_thread
    assert window.selected_task['id'] == item['id']
    assert window.comment_editor.toPlainText() == item['comment']
    assert window.progress_label.text() == 'Готово!'
    assert window.result_card.buttons['published'].isEnabled()


def test_demo_shows_result_but_cannot_change_real_history_or_send(window, service, qtapp):
    before = service.stats()
    window.run_dry()
    wait(qtapp, lambda: window.worker is None)
    assert service.stats() == before
    assert window.selected_task['demo'] is True
    assert not window.demo_banner.isHidden()
    assert not window.result_card.buttons['published'].isEnabled()
    assert not window.result_card.buttons['open'].isEnabled()
    assert window.comment_editor.isReadOnly()
    window.confirm_publication()
    window.send_selected_link()
    assert service.stats() == before
    assert 'демонстрационный' in window.error_label.text().lower()


def test_regeneration_warns_and_decline_avoids_paid_request(window, service, monkeypatch):
    item = task(service)
    window.select_task(item)
    calls = []
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.StandardButton.No)
    monkeypatch.setattr(service, 'prepare_one', lambda *args, **kw: calls.append(args))
    window.regenerate()
    assert calls == []
    assert window.worker is None


def test_review_dialog_asks_only_for_missing_date(window, service):
    item = task(service, published_at=None)
    window.select_task(item)
    window.review_missing()
    dialog = window._review_dialog
    assert len(dialog.findChildren(QLineEdit)) == 1
    assert dialog.findChildren(QPlainTextEdit) == []
    assert not window.result_card.review_button.isHidden()
    assert service.db.get_task(item['id'])['publication_status'] == 'UNCONFIRMED'
    dialog.close()


def test_error_is_readable_and_does_not_send_user_to_configuration_files(window):
    window._error('Не задан OPENAI_API_KEY. Укажите ключ в локальном .env')
    assert 'Настройки' in window.error_label.text()
    assert '.env' not in window.error_label.text()
    assert 'OPENAI_API_KEY' not in window.error_label.text()
    assert 'sk-private-secret' not in safe_error('Ошибка sk-private-secret')


def test_cancelled_pipeline_does_not_show_ready_result(window, qtapp, monkeypatch):
    monkeypatch.setattr(window.service, 'process_url', lambda *args, **kwargs: {'cancelled': True})
    window.url_input.setText('https://www.tiktok.com/@test/video/123456789')
    window.process_url()
    wait(qtapp, lambda: window.worker is None)
    assert window.selected_task is None
    assert 'отменена' in window.progress_label.text()
