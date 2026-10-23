"""The batch desktop drives real local services; external effects stay mocked."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import threading
import time

import httpx
import pytest
from PySide6.QtCore import QEvent
from PySide6.QtGui import QShortcut
from PySide6.QtWidgets import QApplication, QCheckBox, QDialogButtonBox, QMessageBox, QPlainTextEdit

from application import AppService
from config import Config, save_config
from gui.batch_window import BatchWindow
from models import Video


_APP = None


@pytest.fixture(scope='module')
def qtapp():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    _APP.setQuitOnLastWindowClosed(False)
    return _APP


def wait(qtapp, predicate, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        qtapp.processEvents()
        if predicate(): return
        time.sleep(.005)
    raise AssertionError('Qt batch operation did not finish')


@pytest.fixture
def service(tmp_path, monkeypatch):
    for key in ('OPENAI_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'): monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(httpx.Client, 'request', lambda *a, **kw: pytest.fail('unexpected live network'))
    path = tmp_path / 'Папка программы' / 'config.yaml'
    save_config(Config(db_path=str(path.parent / 'data/history.sqlite3')), path)
    app = AppService(path); app.complete_onboarding(); app.batch._pace = lambda *a, **kw: True
    def generate(task_id, context, budget_guard=None):
        return 'реклама: какой скин в RubUP?' if context['account_slot'] == 1 else 'реклама: в рубапе обмен что думаете?'
    app.generator.generate = generate
    yield app
    app.close()


@pytest.fixture
def window(service, qtapp):
    obj = BatchWindow(service); obj.show(); qtapp.processEvents()
    yield obj
    obj.close(); qtapp.processEvents()
    for top in QApplication.topLevelWidgets(): top.close(); top.deleteLater()
    qtapp.sendPostedEvents(None, QEvent.Type.DeferredDelete); qtapp.processEvents()


def seed(service, count=1):
    urls = []
    for index in range(count):
        video_id = str(8880000000000000000 + index); url = f'https://www.tiktok.com/@fixture/video/{video_id}'
        service.db.add_video(Video(video_id, url, description='#cs2 выбор скина', hashtags=['cs2'], published_at='2026-06-01')); urls.append(url)
    return urls


def prepared(window, service):
    imported = service.batch.import_links(seed(service)[0]); service.batch.prepare(imported['batch_id'])
    window.current_batch_id = imported['batch_id']; window.reload_batches()
    return service.batch.groups(imported['batch_id'])[0]


def confirm_dialog(window):
    dialog = window._publication_dialog
    for checkbox in dialog.findChildren(QCheckBox): checkbox.setChecked(True)
    dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()


def test_home_is_multiline_five_sections_four_progress_counters(window):
    assert [b.text() for b in window.nav_buttons] == ['Главная', 'Очередь', 'Аккаунты', 'Настройки', 'Помощь']
    assert window.pages.count() == 5 and isinstance(window.links_input, QPlainTextEdit)
    assert window.prepare_button.text() == 'НАЧАТЬ ПОДГОТОВКУ'
    assert list(window.metrics) == ['videos', 'comments', 'published', 'telegram']
    assert all(label.text() == '0/0' for label in window.metrics.values())
    assert sorted(s.key().toString() for s in window.findChildren(QShortcut)) == ['Ctrl+1', 'Ctrl+2', 'Ctrl+Right']
    assert window.settings_tabs.indexOf(window.image_page) >= 0


def test_fifty_imported_links_persist_before_cost_warning_and_decline_spends_nothing(window, service, qtapp, monkeypatch):
    urls = seed(service, 50); questions = []; calls = []
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: questions.append(a[2]) or QMessageBox.StandardButton.No)
    monkeypatch.setattr(service.batch, 'prepare', lambda *a, **kw: calls.append(a))
    window.links_input.setPlainText('\n'.join(urls)); window.prepare_button.click()
    wait(qtapp, lambda: window.worker is None and bool(questions))
    assert len(service.batch.store.list_items(window.current_batch_id)) == 50
    assert '100' in questions[0] and 'расходовать' in questions[0]
    assert calls == [] and len(service.db.list_tasks()) == 100
    assert window.metrics['videos'].text() == '0/50'
    assert window.metrics['comments'].text() == '0/100'
    assert 'Принято: 50' in window.import_summary.text()
    restored = AppService(service.config_path)
    try:
        second = BatchWindow(restored)
        assert second.links_input.toPlainText().splitlines() == urls
        assert second.current_batch_id == window.current_batch_id
        second.close()
    finally: restored.close()


def test_preparation_is_background_and_creates_two_independent_panels(window, service, qtapp, monkeypatch):
    main_thread = threading.get_ident(); threads = []
    generator = service.generator.generate
    def tracked(*a, **kw): threads.append(threading.get_ident()); return generator(*a, **kw)
    monkeypatch.setattr(service.generator, 'generate', tracked)
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Yes)
    window.links_input.setPlainText(seed(service)[0]); window.prepare_button.click()
    wait(qtapp, lambda: window.worker is None and window.metrics['comments'].text() == '2/2')
    assert len(threads) == 2 and all(t != main_thread for t in threads)
    assert window.pages.currentIndex() == 1 and len(window.queue.cards) == 1
    card = window.queue.cards[0]
    assert set(card.panels) == {1, 2}
    assert card.panels[1]['comment'].toPlainText() != card.panels[2]['comment'].toPlainText()
    assert all(panel['buttons']['publish'].isEnabled() for panel in card.panels.values())
    assert window.metrics['published'].text() == '0/2' and window.metrics['telegram'].text() == '0/1'
    window.copy_current(2)
    assert QApplication.clipboard().text() == card.panels[2]['task']['comment']


def test_one_publication_and_like_do_not_finish_second_account(window, service, monkeypatch):
    service.config.require_like = True
    group = prepared(window, service); first, second = group['tasks']
    window.task_action('publish', first['id']); confirm_dialog(window)
    assert service.db.get_task(first['id'])['publication_status'] == 'PUBLISHED'
    assert service.db.get_task(second['id'])['publication_status'] == 'UNCONFIRMED'
    assert not service.db.get_task(first['id'])['checklist'].get('own_comment_liked')
    assert service.batch.store.telegram_queue() == []
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Yes)
    window.task_action('like', first['id'])
    assert service.db.get_task(first['id'])['checklist']['own_comment_liked'] is True
    assert service.db.get_task(second['id'])['publication_status'] == 'UNCONFIRMED'
    assert service.batch.store.telegram_queue() == []


def test_both_confirmations_start_delivery_worker_without_ui_http(window, service, qtapp, monkeypatch):
    group = prepared(window, service); threads = []
    def drain(batch_id=None):
        threads.append(threading.get_ident())
        for row in service.batch.store.telegram_queue(batch_id): service.batch.store.update_telegram(row['url'], 'SENT')
        return dict(sent=1, failed=0, unknown=0)
    monkeypatch.setattr(service.batch, 'drain_telegram', drain)
    for task in group['tasks']:
        window.task_action('publish', task['id']); confirm_dialog(window)
    wait(qtapp, lambda: window.worker is None and bool(threads))
    assert all(t != threading.get_ident() for t in threads)
    assert all(service.db.get_task(t['id'])['publication_status'] == 'PUBLISHED' for t in group['tasks'])


def test_demonstration_has_fifty_video_cards_hundred_drafts_and_no_real_actions(window, service, qtapp):
    before = service.db.stats(); window.run_demo()
    wait(qtapp, lambda: window.worker is None and window.demo_data is not None, timeout=20)
    assert service.db.stats() == before
    assert len(window.queue.cards) == 50
    assert window.metrics['videos'].text() == '50/50' and window.metrics['comments'].text() == '100/100'
    assert window.metrics['published'].text() == '0/100'
    assert not window.demo_banner.isHidden()
    first = window.queue.cards[0]
    for panel in first.panels.values():
        assert not panel['buttons']['publish'].isEnabled()
        assert not panel['buttons']['like'].isEnabled()
        assert not panel['buttons']['open'].isEnabled()
    task = first.panels[1]['task']; window.task_action('publish', task['id'])
    assert 'демонстрационный' in window.error_label.text().lower()
    assert service.db.stats() == before


def test_pause_resume_stop_have_cooperative_events_and_errors_are_readable(window, qtapp):
    entered, release = threading.Event(), threading.Event()
    def operation(): entered.set(); release.wait(3); raise ValueError('Не задан OPENAI_API_KEY sk-secret')
    window._launch('Проверка', operation); wait(qtapp, entered.is_set)
    window.pause(); assert window.pause_event.is_set()
    window.resume(); assert not window.pause_event.is_set()
    window.stop(); assert window.stop_event.is_set()
    release.set(); wait(qtapp, lambda: window.worker is None)
    assert 'Настройки' in window.error_label.text() and 'sk-secret' not in window.error_label.text()
    assert window.prepare_button.isEnabled()


def test_profile_page_never_claims_authorization_and_saves_independent_names(window, service):
    first = window.accounts.fields[1]; second = window.accounts.fields[2]
    first['username'].setText('first_user'); second['username'].setText('second_user')
    assert window.accounts.save(1) and window.accounts.save(2)
    profiles = service.batch.profiles.list()
    assert profiles[0]['username'] == 'first_user' and profiles[1]['username'] == 'second_user'
    assert all('не подтверждён' in window.accounts.fields[s]['status'].text() for s in (1, 2))


def test_cost_confirmation_shows_saved_batch_limit_not_new_global_setting(window, service, monkeypatch):
    service.config.batch_budget_usd = 1.25
    imported = service.batch.import_links(seed(service)[0]); window.current_batch_id = imported['batch_id']
    service.config.batch_budget_usd = 0.05
    prompts = []
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: prompts.append(args[2]) or QMessageBox.StandardButton.No)
    window._confirm_and_prepare()
    assert '1.2500' in prompts[0] and '0.0500' not in prompts[0]
    assert 'сохранён прежний' in prompts[0]


def test_revoked_comment_visibility_keeps_video_unfinished():
    from gui.batch_queue import task_complete
    row = dict(publication_status='PUBLISHED', checklist={'comment_present': True, 'comment_not_deleted': True})
    assert task_complete(row)
    row['checklist']['comment_not_deleted'] = False
    assert not task_complete(row)
