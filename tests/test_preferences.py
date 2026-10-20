"""Real Qt settings interactions without live API requests or credential leakage."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import threading
import time

import httpx
import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QInputDialog, QLineEdit

from application import AppService
from config import Config, load_config, save_config
from gui.help_page import HELP, HelpPage
from gui.preferences import SettingsPanel, friendly_error
from models import Video


@pytest.fixture(scope='session')
def preferences_app():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    return app


def wait_for(app, predicate):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError('Settings worker did not finish')


@pytest.fixture
def panel(tmp_path, monkeypatch, preferences_app):
    for name in ('OPENAI_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(httpx.Client, 'request', lambda *a, **k: pytest.fail('Live network is forbidden'))
    path = tmp_path / 'config.yaml'
    save_config(Config(db_path=str(tmp_path / 'history.sqlite3')), path)
    service = AppService(path)
    widget = SettingsPanel(service)
    yield widget, service
    widget.wait_for_jobs()
    preferences_app.processEvents()
    widget.close()
    widget.deleteLater()
    preferences_app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    preferences_app.processEvents()
    service.close()


def test_keys_masked_toggle_is_explicit_and_settings_apply_live(panel):
    widget, service = panel
    widget.openai_key.setText('sk-local-test-key')
    assert widget.openai_key.edit.echoMode() == QLineEdit.EchoMode.Password
    widget.openai_key.toggle.click()
    assert widget.openai_key.edit.echoMode() == QLineEdit.EchoMode.Normal
    widget.openai_key.toggle.click()
    assert widget.openai_key.edit.echoMode() == QLineEdit.EchoMode.Password
    widget.telegram_token.setText('123456:test_local_token')
    widget.chat_id.setText('42')
    widget.telegram_mode.setCurrentIndex(widget.telegram_mode.findData('published'))
    assert widget.save()
    assert service.generator.api_key == 'sk-local-test-key'
    assert service.telegram._token == '123456:test_local_token'
    assert service.config.telegram_mode == 'published'
    assert not widget.last_save_result['restart_required']
    yaml = service.config_path.read_text()
    assert 'sk-local-test-key' not in yaml and 'test_local_token' not in yaml
    assert load_config(service.config_path).telegram_mode == 'published'


def test_model_probe_background_uses_unsaved_key_and_only_actual_results(panel, preferences_app, monkeypatch):
    widget, service = panel
    entered, release = threading.Event(), threading.Event()
    threads = []
    def models(api_key=None):
        threads.append(threading.get_ident())
        assert api_key == 'candidate-key'
        entered.set()
        release.wait(3)
        return ['gpt-4.1-mini', 'gpt-4.1-nano']
    monkeypatch.setattr(service.settings, 'models', models)
    widget.openai_key.setText('candidate-key')
    widget.check_openai()
    try:
        wait_for(preferences_app, entered.is_set)
        assert threads[0] != threading.get_ident()
        assert widget.busy
        assert 'принят' not in widget.ai_status.text()
        assert not (service.root / '.env').exists()
    finally:
        release.set()
    wait_for(preferences_app, lambda: not widget.busy)
    assert [widget.model_combo.itemData(i) for i in range(widget.model_combo.count())] == ['gpt-4.1-mini', 'gpt-4.1-nano']
    widget.select_economical()
    assert widget.model_combo.currentData() == 'gpt-4.1-nano'
    assert 'неизвестна' in widget.cost_label.text()
    assert service.config.model == 'gpt-4.1-mini'  # Probe does not implicitly persist.
    widget.openai_key.setText('another-key')
    assert widget._available_models == []
    assert widget.model_combo.count() == 1
    assert 'не проверена' in widget.model_combo.currentText()


def test_bot_check_sends_nothing_and_never_claims_chat_rights(panel, preferences_app, monkeypatch):
    widget, service = panel
    calls, sent = [], []
    monkeypatch.setattr(service.settings, 'test_telegram', lambda **kw: calls.append(kw) or True)
    monkeypatch.setattr(service, 'send_test_link', lambda url: sent.append(url) or True)
    widget.telegram_token.setText('123:local_token')
    widget.chat_id.setText('99')
    widget.check_telegram()
    wait_for(preferences_app, lambda: not widget.busy)
    assert calls == [{'token': '123:local_token', 'chat_id': '99'}]
    assert sent == []
    assert 'Доставка' in widget.telegram_status.text()
    assert 'ещё не проверена' in widget.telegram_status.text()


def test_test_link_requires_explicit_input_and_real_task(panel, preferences_app, monkeypatch):
    widget, service = panel
    sent = []
    monkeypatch.setattr(service, 'send_test_link', lambda url: sent.append(url) or True)
    widget.send_test_link()
    assert sent == []
    assert 'реальное видео' in widget.telegram_status.text()
    url = 'https://www.tiktok.com/@user/video/111'
    service.db.add_video(Video('111', url, description='#cs2', hashtags=['cs2'], published_at='2026-06-01'))
    service.db.ensure_task(service.config.account_id, '111')
    widget.telegram_token.setText('123:local_token')
    widget.chat_id.setText('99')
    monkeypatch.setattr(QInputDialog, 'getText', lambda *a, **k: ('', False))
    widget.send_test_link()
    assert sent == []
    monkeypatch.setattr(QInputDialog, 'getText', lambda *a, **k: (url, True))
    widget.send_test_link()
    wait_for(preferences_app, lambda: not widget.busy)
    assert sent == [url]


def test_error_messages_do_not_expose_keys_or_technical_setup_files():
    message = friendly_error(ValueError('Bad sk-super-secret and 123:token_secret'))
    assert 'sk-super-secret' not in message and 'token_secret' not in message
    assert '.env' not in friendly_error(ValueError('Не задан OPENAI_API_KEY. Укажите .env'))
    assert 'Настройки' in friendly_error(ValueError('Не задан OPENAI_API_KEY'))
    assert 'Ключ не подошёл' in friendly_error(ValueError('HTTP 401'))
    assert 'ограничил' in friendly_error(ValueError('HTTP 429'))


def test_help_contains_complete_local_guides(preferences_app):
    widget = HelpPage()
    text = '\n'.join(html for _, html in HELP)
    for subject in ('BotFather', 'getUpdates', 'ChatGPT', 'предварительный', 'картинки', 'Chat ID'):
        assert subject.casefold() in text.casefold()
    assert widget.topics.count() == 6
    widget.topics.setCurrentRow(4)
    assert widget.pages.currentIndex() == 4
    widget.close()
    widget.deleteLater()
    preferences_app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
