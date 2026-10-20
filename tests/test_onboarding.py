"""Setup is optional, honest about checks and remembered after completion."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import hashlib
import time
import threading

import httpx
from PIL import Image
import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QFileDialog

from application import AppService
from config import Config, save_config
from gui.onboarding import SetupWizard


@pytest.fixture(scope='session')
def wizard_app():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    return app


def wait_for(app, predicate):
    end = time.monotonic() + 4
    while time.monotonic() < end:
        app.processEvents()
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError('Setup operation did not complete')


@pytest.fixture
def setup(tmp_path, monkeypatch, wizard_app):
    for name in ('OPENAI_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(httpx.Client, 'request', lambda *a, **k: pytest.fail('Unexpected live network'))
    path = tmp_path / 'config.yaml'
    save_config(Config(db_path=str(tmp_path / 'history.sqlite3')), path)
    service = AppService(path)
    wizard = SetupWizard(service)
    wizard.show()
    wizard_app.processEvents()
    yield wizard, service
    wizard.wait_for_jobs()
    wizard_app.processEvents()
    wizard.reject()
    wizard.deleteLater()
    wizard_app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    wizard_app.processEvents()
    service.close()


def test_five_steps_can_be_skipped_and_completion_persists(setup, wizard_app):
    wizard, service = setup
    assert wizard.pageIds() == [0, 1, 2, 3, 4]
    assert not service.onboarding_complete()
    wizard.next()
    assert wizard.currentId() == 1
    assert wizard.ai_skip.isChecked()
    wizard.next()
    assert wizard.currentId() == 2
    assert wizard.telegram_skip.isChecked()
    wizard.next()
    wizard.next()
    assert wizard.currentId() == 4
    assert 'не подключён' in wizard.finish_summary.text()
    demos = []
    wizard.demo_requested.connect(lambda: demos.append(True))
    wizard.accept()
    assert service.onboarding_complete()
    assert demos == [True]
    restored = AppService(service.config_path)
    try:
        assert restored.onboarding_complete()
    finally:
        restored.close()


def test_cancel_does_not_claim_setup_completed(setup):
    wizard, service = setup
    wizard.reject()
    assert not service.onboarding_complete()


def test_success_requires_actual_response_and_does_not_send(setup, wizard_app, monkeypatch):
    wizard, service = setup
    monkeypatch.setattr(service.settings, 'models', lambda api_key=None: ['gpt-4.1-mini'])
    wizard.next()
    wizard.openai_key.setText('candidate-key')
    assert not wizard._openai_verified
    wizard.check_openai()
    wait_for(wizard_app, lambda: not wizard.busy)
    assert wizard._openai_verified
    wizard.next()
    assert service.generator.api_key == 'candidate-key'
    calls = []
    monkeypatch.setattr(service.settings, 'test_telegram', lambda **kw: calls.append(kw) or True)
    wizard.telegram_token.setText('123:local_token')
    wizard.chat_id.setText('42')
    assert not wizard._telegram_verified
    wizard.check_telegram()
    wait_for(wizard_app, lambda: not wizard.busy)
    assert wizard._telegram_verified
    assert calls == [{'token': '123:local_token', 'chat_id': '42'}]
    assert service.db.stats()['telegram_sent'] == 0
    wizard.next()
    assert service.config.telegram_mode == 'published'
    wizard.next()
    assert 'доставка в чат ещё не проверена' in wizard.finish_summary.text()


def test_cannot_complete_while_connection_worker_is_active(setup, wizard_app, monkeypatch):
    wizard, service = setup
    entered, release = threading.Event(), threading.Event()
    def models(api_key=None):
        entered.set()
        release.wait(3)
        return ['gpt-4.1-mini']
    monkeypatch.setattr(service.settings, 'models', models)
    wizard.next()
    wizard.openai_key.setText('candidate')
    wizard.check_openai()
    try:
        wait_for(wizard_app, entered.is_set)
        wizard.reject()
        assert wizard.isVisible()
        assert not service.onboarding_complete()
    finally:
        release.set()
    wait_for(wizard_app, lambda: not wizard.busy)
    assert not wizard.isVisible()
    assert not service.onboarding_complete()


def test_wizard_image_import_preserves_original_bytes(setup, tmp_path, monkeypatch):
    wizard, service = setup
    original = tmp_path / 'оригинал с пробелом.png'
    Image.new('RGB', (9, 7), 'green').save(original)
    before = original.read_bytes()
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *a, **k: (str(original), ''))
    wizard.choose_image(3)
    image = service.images.get_image(3)
    assert image['sha256'] == hashlib.sha256(before).hexdigest()
    assert original.read_bytes() == before
    assert service.images.verify(3)
    assert 'оригинал добавлен' in wizard.image_labels[3].text()


def test_demo_shortcut_does_not_generate_or_send_itself(setup):
    wizard, service = setup
    before = service.db.stats()
    demos = []
    wizard.demo_requested.connect(lambda: demos.append(True))
    wizard.start_demo()
    assert demos == [True]
    assert service.db.stats() == before
    assert service.onboarding_complete()
