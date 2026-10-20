"""Cards show independent outcomes and keep original image bytes unchanged."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from config import Config, load_config, save_config
from gui.image_page import ImagePage
from gui.task_cards import TaskCards, status_label
from image_manager import ImageManager


@pytest.fixture(scope="module")
def qtapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.fixture(autouse=True)
def cleanup(qtapp):
    yield
    for widget in QApplication.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    qtapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qtapp.processEvents()


@pytest.fixture
def image_service(tmp_path):
    service = SimpleNamespace(config=Config(), images=ImageManager(tmp_path / "images"))
    def save(config):
        save_config(config, tmp_path / "config.yaml")
        service.config = load_config(tmp_path / "config.yaml")
        return {"restart_required": False}
    service.save_settings = save
    return service


def test_telegram_link_does_not_infer_completed_task():
    task = {"telegram_status": "SENT", "publication_status": "UNCONFIRMED"}
    assert status_label(task) == "Ссылка отправлена"
    assert status_label({**task, "report_status": "REPORT_ACCEPTED"}) == "Отчёт принят"
    assert status_label({**task, "report_status": "REPORT_ACCEPTED", "reward_status": "REWARD_CONFIRMED"}) == "Задание завершено"


def test_task_cards_filter_actual_states_and_keep_task_identity(qtapp):
    rows = [
        {"id": 1, "author": "alice", "comment": "RubUP вопрос", "processing_status": "PENDING_APPROVAL", "publication_status": "UNCONFIRMED"},
        {"id": 2, "author": "bob", "comment": "RubUP ответ", "processing_status": "PUBLISHED", "publication_status": "PUBLISHED"},
        {"id": 3, "author": "carol", "processing_status": "FAILED"},
    ]
    cards = TaskCards()
    opened, resumed = [], []
    cards.open_task.connect(lambda task: opened.append(task["id"]))
    cards.resume_task.connect(lambda task: resumed.append(task["id"]))
    cards.set_tasks(rows)
    cards.set_filter("wait")
    assert [card.task["id"] for card in cards.cards] == [1]
    cards.cards[0].open_button.click()
    cards.cards[0].resume_button.click()
    assert opened == [1] and resumed == [1]
    cards.set_filter("published")
    assert len(cards.cards) == 1 and not cards.cards[0].resume_button.isEnabled()
    cards.set_filter("errors")
    assert [card.task["id"] for card in cards.cards] == [3]
    cards.set_filter("all")
    cards.set_query("BOB")
    assert [card.task["id"] for card in cards.cards] == [2]


def test_image_card_preserves_original_and_updates_add_replace(qtapp, image_service, tmp_path):
    original = tmp_path / "картинка с пробелами.png"
    Image.new("RGB", (32, 20), "red").save(original)
    page = ImagePage(image_service)
    assert page.buttons[1].text() == "Добавить"
    before = original.read_bytes()
    assert page.add_image(1, original)
    assert original.read_bytes() == before
    assert Path(image_service.images.get_image(1)["path"]).read_bytes() == before
    assert page.buttons[1].text() == "Заменить"
    assert "32 × 20" in page.descriptions[1].text()
    page.set_busy(True)
    assert not page.buttons[1].isEnabled()
    page.set_busy(False)
    assert page.buttons[1].isEnabled()


def test_large_history_is_loaded_in_small_visible_batches(qtapp):
    cards = TaskCards()
    cards.set_tasks([{"id": number, "comment": f"Вариант RubUP {number}"} for number in range(100)])
    assert len(cards.cards) == 40
    cards._show_more()
    assert len(cards.cards) == 80
    cards.set_query("Вариант RubUP 99")
    assert len(cards.cards) == 1
    assert cards.cards[0].task["id"] == 99


def test_invalid_image_error_is_visible_and_existing_original_is_preserved(qtapp, image_service, tmp_path):
    original, broken = tmp_path / "original.png", tmp_path / "broken.png"
    Image.new("RGB", (10, 10), "blue").save(original)
    broken.write_bytes(b"not a valid png")
    page = ImagePage(image_service)
    assert page.add_image(1, original)
    errors = []
    page.failed.connect(errors.append)
    assert not page.add_image(1, broken)
    assert errors
    assert Path(image_service.images.get_image(1)["path"]).read_bytes() == original.read_bytes()


def test_mode_and_order_are_applied_without_attaching_images(qtapp, image_service):
    page = ImagePage(image_service)
    page.mode_combo.setCurrentIndex(page.mode_combo.findData("rotate"))
    page.order_edit.setText("4,3,2,1")
    assert page.apply_settings()
    assert image_service.config.image_mode == "rotate"
    assert image_service.config.image_order == [4, 3, 2, 1]
    assert image_service.config.require_image is False
    assert image_service.images.usage_history() == []
    assert "сохранён" in page.feedback.text()
