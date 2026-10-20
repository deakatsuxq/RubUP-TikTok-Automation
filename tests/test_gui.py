"""Offscreen UI tests exercise task selection, edits, filters and workers."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import replace
from pathlib import Path
import threading
import time

import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QEvent

from application import AppService
from config import Config, save_config
from gui.main_window import MainWindow
from gui.settings_window import SettingsWindow
from gui.video_table import VideoTable


@pytest.fixture(scope="module")
def qtapp():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    yield app
    app.closeAllWindows()
    app.processEvents()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture(autouse=True)
def cleanup_qt_widgets(qtapp):
    yield
    for widget in QApplication.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    qtapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qtapp.processEvents()


@pytest.fixture
def service(tmp_path):
    path = tmp_path / "config.yaml"
    save_config(Config(db_path=str(tmp_path / "history.sqlite3")), path)
    obj = AppService(path)
    yield obj
    obj.close()


def wait_for(qtapp, predicate, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        qtapp.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Qt worker did not finish")


def test_table_filters_and_sorted_selection_keep_task_identity(qtapp):
    table = VideoTable()
    rows = [
        {"id": 20, "video_id": "200", "author": "alice", "processing_status": "NEW"},
        {"id": 10, "video_id": "100", "author": "bob", "processing_status": "PENDING_APPROVAL"},
    ]
    selected = []
    table.task_selected.connect(lambda row: selected.append(row["id"]))
    table.set_tasks(rows)
    table.proxy.set_query("ALICE")
    assert table.proxy.rowCount() == 1
    table.selectRow(0)
    assert selected[-1] == 20
    table.proxy.set_query("")
    table.proxy.set_status("PENDING_APPROVAL")
    assert table.proxy.rowCount() == 1
    table.selectRow(0)
    assert selected[-1] == 10
    table.close()


def test_window_import_select_edit_and_copy(qtapp, service):
    service.import_urls("https://www.tiktok.com/@alice/video/123456789")
    window = MainWindow(service)
    tasks = service.list_tasks()
    assert len(tasks) == 1
    window.video_table.selectRow(0)
    assert window.selected_task["video_id"] == "123456789"
    window.date_field.setText("2026-06-12")
    window.description_field.setPlainText("#cs2 обзор скинов")
    window.save_metadata()
    window.comment_editor.setPlainText("реклама: какие скины в RubUP?")
    window.save_comment()
    assert service.list_tasks()[0]["comment"] == "реклама: какие скины в RubUP?"
    window.copy_comment()
    assert QApplication.clipboard().text() == "реклама: какие скины в RubUP?"
    assert window.dashboard.labels["drafts"].text() == "1"
    assert window.dashboard.labels["published"].text() == "0"
    window.search_field.setText("nonexistent")
    assert window.video_table.proxy.rowCount() == 0
    window.clear_filters()
    assert window.video_table.proxy.rowCount() == 1
    window.close()


def test_network_worker_does_not_block_ui_and_close_is_deferred(qtapp, service):
    window = MainWindow(service)
    entered = threading.Event()
    release = threading.Event()
    worker_thread = []

    def operation():
        worker_thread.append(threading.get_ident())
        entered.set()
        release.wait(2)
        return {"done": True}

    assert window._launch("Test", operation)
    wait_for(qtapp, entered.is_set)
    assert worker_thread[0] != threading.get_ident()
    assert not window.process_button.isEnabled()
    window.close()
    assert window.stop_event.is_set()
    assert window.worker is not None
    release.set()
    wait_for(qtapp, lambda: window.worker is None)
    assert window.process_button.isEnabled()


def test_pause_resume_stop_use_events(qtapp, service):
    window = MainWindow(service)
    window.pause_processing()
    assert window.pause_event.is_set()
    window.resume_processing()
    assert not window.pause_event.is_set()
    window.pause_processing()
    window.stop_processing()
    assert window.stop_event.is_set()
    assert not window.pause_event.is_set()
    window.close()


def test_settings_collect_and_persist_non_secret_values(qtapp, service):
    dialog = SettingsWindow(service)
    dialog.fields["hashtags"].setPlainText("cs2\ndota2")
    dialog.fields["disabled_hashtags"].setPlainText("dota2")
    dialog.fields["image_order"].setText("4,3,2,1")
    dialog.fields["input_price_per_million"].setText("0.42")
    values = dialog.values()
    assert values["hashtags"] == ["cs2", "dota2"]
    assert values["disabled_hashtags"] == ["dota2"]
    assert values["image_order"] == [4, 3, 2, 1]
    assert values["input_price_per_million"] == 0.42
    assert "OPENAI_API_KEY" not in values
    dialog._save()
    from config import load_config
    saved = load_config(service.config_path)
    assert saved.image_order == [4, 3, 2, 1]
    assert saved.enabled_hashtags == ["cs2"]
    assert saved.input_price_per_million == 0.42
    dialog.close()


def test_copy_rejects_unvalidated_text_and_changed_original_is_blocked(qtapp, service, tmp_path, monkeypatch):
    from PIL import Image
    from PySide6.QtGui import QDesktopServices
    service.import_urls("https://www.tiktok.com/@alice/video/123456789")
    task_id = service.list_tasks()[0]["id"]
    service.update_metadata(task_id, "2026-06-12", "#cs2 скины", "alice")
    first = tmp_path / "original.png"
    second = tmp_path / "replacement.png"
    Image.new("RGB", (3, 3), "red").save(first)
    Image.new("RGB", (3, 3), "blue").save(second)
    service.images.set_image(1, first)
    service.config.image_mode = "task"
    service.set_task_requirements(task_id, True, 1)
    service.save_comment(task_id, "реклама: какие скины в RubUP?")
    window = MainWindow(service)
    window.video_table.selectRow(0)
    errors = []
    window._error = errors.append
    QApplication.clipboard().setText("untouched")
    window.comment_editor.setPlainText("нет обязательного бренда")
    window.copy_comment()
    assert QApplication.clipboard().text() == "untouched"
    assert errors
    service.images.set_image(1, second)
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url))
    window.open_image()
    assert opened == []
    assert "заменён" in errors[-1]
    window.close()
