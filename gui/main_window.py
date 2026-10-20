"""Responsive local task preparation UI. No automatic TikTok publishing."""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from PySide6.QtCore import QThread, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSplitter,
    QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)
from gui.dashboard import Dashboard
from gui.settings_window import SettingsWindow
from gui.video_table import LABELS, VideoTable

STYLE = """
QMainWindow, QDialog { background: #10141d; }
QWidget { color: #e7ecf4; font-family: 'Segoe UI', sans-serif; font-size: 12px; }
QLabel#brand { font-size: 27px; font-weight: 700; letter-spacing: 1px; }
QLabel#muted { color: #95a4b8; }
QLabel#modeBadge { color: #67dfc0; background: #17352f; border-radius: 7px; padding: 7px 11px; }
QLabel#metricValue { font-size: 25px; font-weight: 600; color: #f2f5fb; }
QLabel#sectionTitle { font-size: 15px; font-weight: 600; }
QFrame#metricCard, QFrame#imageCard { background: #192130; border: 1px solid #2a3547; border-radius: 9px; }
QPushButton { background: #263347; border: 1px solid #37465d; border-radius: 6px; padding: 8px 12px; }
QPushButton:hover { background: #34465f; border-color: #61799c; }
QPushButton:pressed { background: #182d42; }
QPushButton:disabled { color: #617087; background: #1b2330; border-color: #273145; }
QPushButton#primary { background: #1f9d80; color: #fff; border-color: #26ba98; font-weight: 600; }
QPushButton#primary:hover { background: #27b292; }
QPushButton#danger { color: #ffb4ba; }
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox { background: #151c29; border: 1px solid #344158; border-radius: 5px; padding: 6px; selection-background-color: #245e72; }
QLineEdit:focus, QPlainTextEdit:focus { border-color: #40bda2; }
QComboBox QAbstractItemView { background: #1c2738; selection-background-color: #2c4b67; }
QTableView { background: #151c29; alternate-background-color: #192231; border: 1px solid #303d52; border-radius: 5px; gridline-color: #263347; selection-background-color: #244d61; }
QHeaderView::section { background: #223047; color: #b9c8dc; padding: 8px; border: 0px; border-right: 1px solid #344158; }
QTabWidget::pane { border: 1px solid #2d394b; border-radius: 6px; background: #131a26; }
QTabBar::tab { background: #1b2535; padding: 10px 17px; border: 0; color: #a8b7ca; }
QTabBar::tab:selected { background: #26384c; color: #70e0c6; }
QCheckBox { spacing: 7px; }
QCheckBox::indicator { width: 16px; height: 16px; }
QScrollArea { border: 0; }
QWidget#editorPane, QWidget#detailPane { background: #131a26; }
QStatusBar { color: #a4b7ce; background: #192130; }
QSplitter::handle { background: #263347; }
QScrollBar:vertical { background: #131a26; width: 11px; }
QScrollBar:horizontal { background: #131a26; height: 11px; }
QScrollBar::handle { background: #3c4d65; border-radius: 4px; min-height: 25px; min-width: 25px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: #131a26; }
"""


def safe_error(exc):
    """Avoid exposing API keys even if an external exception includes a URL."""
    message = str(exc)
    message = re.sub(r"/bot[^/\s]+", "/bot[СКРЫТО]", message)
    message = re.sub(r"\bsk-[\w-]+", "[СКРЫТО]", message)
    return message[:1200]


class Worker(QThread):
    result_ready = Signal(object)
    failed = Signal(str)
    progress = Signal(object)

    def __init__(self, function, parent=None):
        super().__init__(parent)
        self.function = function

    def run(self):
        try:
            self.result_ready.emit(self.function())
        except Exception as exc:
            self.failed.emit(safe_error(exc))


class MainWindow(QMainWindow):
    def __init__(self, service):
        super().__init__()
        self.service = service
        self.worker = None
        self.selected_task = None
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self._periodic = QTimer(self)
        self._periodic.timeout.connect(self._periodic_refresh)
        self._mutation_controls = []
        self.setWindowTitle("RubUP TikTok Automation • подготовка заданий")
        self.resize(1440, 1000)
        self.setMinimumSize(1000, 760)
        self.setStyleSheet(STYLE)
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 15, 20, 12)
        root.setSpacing(12)
        header = QHBoxLayout()
        brand = QLabel("RubUP")
        brand.setObjectName("brand")
        header.addWidget(brand)
        caption = QLabel("TIKTOK AUTOMATION\nЛокальная подготовка · ручная публикация")
        caption.setObjectName("muted")
        header.addWidget(caption)
        header.addStretch()
        self.mode_badge = QLabel()
        self.mode_badge.setObjectName("modeBadge")
        header.addWidget(self.mode_badge)
        self.settings_button = self._button("Настройки", self.open_settings)
        header.addWidget(self.settings_button)
        root.addLayout(header)
        self.dashboard = Dashboard()
        root.addWidget(self.dashboard)
        notice = QLabel("Источники: ваши ссылки и CSV/JSON · неизвестная дата требует ручной проверки · публикация, отчёт и вознаграждение подтверждаются отдельно")
        notice.setObjectName("muted")
        notice.setWordWrap(True)
        root.addWidget(notice)
        controls = QHBoxLayout()
        self.search_start = self._button("Начать поиск", self.start_search)
        controls.addWidget(self.search_start)
        self.search_stop = self._button("Остановить поиск", self.stop_search, mutation=False)
        controls.addWidget(self.search_stop)
        self.process_button = self._button("Запустить обработку", self.start_processing, primary=True)
        controls.addWidget(self.process_button)
        self.pause_button = self._button("Пауза", self.pause_processing, mutation=False)
        controls.addWidget(self.pause_button)
        self.resume_button = self._button("Продолжить", self.resume_processing, mutation=False)
        controls.addWidget(self.resume_button)
        self.stop_button = self._button("Остановить", self.stop_processing, mutation=False)
        self.stop_button.setObjectName("danger")
        controls.addWidget(self.stop_button)
        controls.addStretch()
        controls.addWidget(self._button("Запустить dry-run", self.run_dry))
        root.addLayout(controls)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self._build_workspace()
        self._build_images()
        self._build_history()
        self._build_connections()
        self.statusBar().showMessage("Готово к работе. Импортируйте ссылки на видео.")
        self.reload()
        self._set_busy(False)

    def _button(self, text, callback, primary=False, mutation=True):
        button = QPushButton(text)
        button.clicked.connect(callback)
        if primary:
            button.setObjectName("primary")
        if mutation:
            self._mutation_controls.append(button)
        return button

    def _build_workspace(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        bar = QHBoxLayout()
        bar.addWidget(self._button("+ Добавить URL", self.import_urls))
        bar.addWidget(self._button("Импорт TXT / CSV / JSON", self.import_file))
        self.search_field = QLineEdit()
        self.search_field.setPlaceholderText("Фильтр по ID, автору, хештегу или комментарию…")
        bar.addWidget(self.search_field, 1)
        self.status_filter = QComboBox()
        self.status_filter.addItem("Все статусы", "")
        for status in ("NEW", "ANALYZED", "DRAFT_READY", "PENDING_APPROVAL", "PUBLISHED", "FAILED", "SKIPPED"):
            self.status_filter.addItem(LABELS[status], status)
        bar.addWidget(self.status_filter)
        bar.addWidget(self._button("Очистить фильтры", self.clear_filters, mutation=False))
        layout.addLayout(bar)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.video_table = VideoTable()
        self.video_table.task_selected.connect(self.select_task)
        self.search_field.textChanged.connect(self.video_table.proxy.set_query)
        self.status_filter.currentIndexChanged.connect(lambda _: self.video_table.proxy.set_status(self.status_filter.currentData()))
        splitter.addWidget(self.video_table)
        editor_scroll = QScrollArea()
        editor_scroll.setWidgetResizable(True)
        editor = QWidget()
        editor.setObjectName("editorPane")
        details = QVBoxLayout(editor)
        self.task_title = QLabel("Выберите видео в таблице")
        self.task_title.setObjectName("sectionTitle")
        details.addWidget(self.task_title)
        self.detail_tabs = QTabWidget()
        details.addWidget(self.detail_tabs)
        comment_page = QWidget()
        comment_page.setObjectName("detailPane")
        metadata_page = QWidget()
        metadata_page.setObjectName("detailPane")
        self.detail_tabs.addTab(comment_page, "Комментарий и выполнение")
        self.detail_tabs.addTab(metadata_page, "Метаданные и требования задания")
        details = QVBoxLayout(comment_page)
        metadata_layout = QVBoxLayout(metadata_page)
        row = QHBoxLayout()
        self.author_field = QLineEdit()
        self.author_field.setPlaceholderText("Автор")
        self.date_field = QLineEdit()
        self.date_field.setPlaceholderText("Дата: ГГГГ-ММ-ДД или пусто")
        row.addWidget(self.author_field)
        row.addWidget(self.date_field)
        self.metadata_button = self._button("Сохранить проверенные метаданные", self.save_metadata)
        row.addWidget(self.metadata_button)
        metadata_layout.addLayout(row)
        requirements = QHBoxLayout()
        self.require_image = QCheckBox("Задание требует картинку")
        self.require_like = QCheckBox("Задание требует лайк")
        self.required_image_slot = QComboBox()
        self.required_image_slot.addItem("По текущему режиму выбора", None)
        for slot in range(1, 5):
            self.required_image_slot.addItem(f"Обязательный оригинал {slot}", slot)
        requirements.addWidget(self.require_image)
        requirements.addWidget(self.required_image_slot)
        requirements.addWidget(self.require_like)
        requirements.addWidget(self._button("Сохранить требования задания", self.save_requirements))
        requirements.addStretch()
        metadata_layout.addLayout(requirements)
        self.description_field = QPlainTextEdit()
        self.description_field.setPlaceholderText("Описание и #хештеги из видео. Дату и описание вводите после ручной проверки источника.")
        self.description_field.setMaximumHeight(75)
        metadata_layout.addWidget(self.description_field)
        metadata_layout.addStretch()
        self.comment_editor = QPlainTextEdit()
        self.comment_editor.setPlaceholderText("Здесь появится подготовленный комментарий. Это ещё не публикация.")
        self.comment_editor.setMaximumHeight(90)
        self.comment_editor.textChanged.connect(self._comment_length)
        details.addWidget(self.comment_editor)
        self.comment_hint = QLabel("0 символов")
        self.comment_hint.setObjectName("muted")
        details.addWidget(self.comment_hint)
        actions = QHBoxLayout()
        actions.addWidget(self._button("Сгенерировать заново", self.regenerate))
        actions.addWidget(self._button("Проверить и сохранить", self.save_comment))
        actions.addWidget(self._button("Скопировать", self.copy_comment, mutation=False))
        actions.addWidget(self._button("Открыть TikTok", self.open_video, mutation=False))
        actions.addWidget(self._button("Открыть картинку", self.open_image, mutation=False))
        actions.addWidget(self._button("Метаданные oEmbed", self.enrich_metadata))
        details.addLayout(actions)
        checklist = QGridLayout()
        self.checks = {}
        items = [
            ("published", "Я опубликовал(а) комментарий"),
            ("comment_present", "Комментарий опубликован и существует"),
            ("image_attached", "Требуемая картинка прикреплена"),
            ("own_comment_liked", "Лайк своему комментарию поставлен"),
            ("comment_not_deleted", "Комментарий не удалён (проверено)"),
        ]
        for i, (name, caption) in enumerate(items):
            control = QCheckBox(caption)
            self.checks[name] = control
            checklist.addWidget(control, i // 3, i % 3)
        details.addLayout(checklist)
        confirmations = QHBoxLayout()
        confirmations.addWidget(self._button("Сохранить чек-лист", self.save_checklist))
        confirmations.addWidget(self._button("Подтвердить ручную публикацию", self.confirm_publication, primary=True))
        confirmations.addWidget(self._button("Отчёт подан", self.confirm_report_submitted))
        confirmations.addWidget(self._button("Отчёт принят", self.confirm_report_accepted))
        confirmations.addWidget(self._button("Начисление подтверждено", self.confirm_reward))
        details.addLayout(confirmations)
        self.task_details = QLabel()
        self.task_details.setWordWrap(True)
        self.task_details.setObjectName("muted")
        details.addWidget(self.task_details)
        editor_scroll.setWidget(editor)
        splitter.addWidget(editor_scroll)
        splitter.setSizes([280, 420])
        layout.addWidget(splitter, 1)
        self.tabs.addTab(tab, "Видео и комментарии")

    def _build_images(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        text = QLabel("Добавьте четыре оригинала JPG/JPEG/PNG. Файлы копируются без перекодирования; превью не меняет оригинал. Режим и порядок выбора задаются в настройках.")
        text.setWordWrap(True)
        text.setObjectName("muted")
        layout.addWidget(text)
        cards = QGridLayout()
        self.image_previews = {}
        self.image_descriptions = {}
        for slot in range(1, 5):
            card = QFrame()
            card.setObjectName("imageCard")
            box = QVBoxLayout(card)
            title = QLabel(f"Оригинал {slot}")
            title.setObjectName("sectionTitle")
            box.addWidget(title)
            preview = QLabel("Файл не добавлен")
            preview.setMinimumSize(200, 150)
            preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            box.addWidget(preview, 1)
            description = QLabel()
            description.setWordWrap(True)
            description.setObjectName("muted")
            box.addWidget(description)
            box.addWidget(self._button("Выбрать оригинал…", lambda checked=False, s=slot: self.choose_image(s)))
            self.image_previews[slot] = preview
            self.image_descriptions[slot] = description
            cards.addWidget(card, (slot - 1) // 2, (slot - 1) % 2)
        layout.addLayout(cards, 1)
        self.tabs.addTab(tab, "4 изображения")

    def _build_history(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        buttons = QHBoxLayout()
        buttons.addWidget(self._button("Обновить историю", self.reload, mutation=False))
        buttons.addWidget(self._button("Экспорт истории CSV", self.export_csv, mutation=False))
        buttons.addStretch()
        layout.addLayout(buttons)
        self.cost_summary = QLabel()
        self.cost_summary.setWordWrap(True)
        self.cost_summary.setObjectName("muted")
        layout.addWidget(self.cost_summary)
        self.history = QTextEdit()
        self.history.setReadOnly(True)
        layout.addWidget(self.history, 1)
        self.tabs.addTab(tab, "История и расходы")

    def _build_connections(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        title = QLabel("Проверка подключений")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        note = QLabel("OpenAI читает доступность модели; Telegram вызывает getMe без отправки сообщения. Реальная генерация может расходовать средства. Telegram отправляет только прямую ссылку, по одной на сообщение.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(note)
        row = QHBoxLayout()
        row.addWidget(self._button("Проверить OpenAI", lambda: self._launch("Проверка OpenAI", self.service.test_openai, self._connection_result)))
        row.addWidget(self._button("Проверить Telegram", lambda: self._launch("Проверка Telegram", self.service.test_telegram, self._connection_result)))
        row.addStretch()
        layout.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(self._button("Отправить подходящие ссылки Telegram", self.send_links, primary=True))
        row.addStretch()
        layout.addLayout(row)
        self.connection_log = QPlainTextEdit()
        self.connection_log.setReadOnly(True)
        layout.addWidget(self.connection_log, 1)
        self.tabs.addTab(tab, "Подключения")

    def _connection_result(self, result):
        self.connection_log.appendPlainText("Соединение проверено: " + ("успешно" if result else "не подтверждено"))

    def _task_id(self):
        if not self.selected_task:
            raise ValueError("Сначала выберите видео в таблице.")
        return self.selected_task["id"]

    def _local(self, function):
        try:
            result = function()
            self.reload()
            return result
        except Exception as exc:
            self._error(safe_error(exc))
            return None

    def _error(self, text):
        self.statusBar().showMessage(text)
        self.connection_log.appendPlainText(text)
        QMessageBox.warning(self, "Действие не выполнено", text)

    def _set_busy(self, busy):
        for button in self._mutation_controls:
            button.setEnabled(not busy)
        self.pause_button.setEnabled(busy and not self.pause_event.is_set())
        self.resume_button.setEnabled(busy and self.pause_event.is_set())
        self.stop_button.setEnabled(busy)
        self.search_stop.setEnabled(self._periodic.isActive())
        self.comment_editor.setReadOnly(busy)
        for field in (self.author_field, self.date_field, self.description_field):
            field.setEnabled(not busy)
        for control in list(self.checks.values()) + [self.require_image, self.require_like, self.required_image_slot]:
            control.setEnabled(not busy)

    def _launch(self, title, function, callback=None):
        if self.worker is not None:
            self.statusBar().showMessage("Дождитесь завершения текущей операции.")
            return False
        self.stop_event.clear()
        self.pause_event.clear()
        self.worker = Worker(function, self)
        self.worker.result_ready.connect(lambda result: self._completed(title, result, callback))
        self.worker.failed.connect(self._error)
        self.worker.progress.connect(self._progress)
        self.worker.finished.connect(self._finished)
        self._set_busy(True)
        self.statusBar().showMessage(title + "…")
        self.worker.start()
        return True

    def _progress(self, result):
        self.reload()
        self.statusBar().showMessage(
            f"Подготовка: {result.get('prepared', 0)} готово · {result.get('failed', 0)} ошибок · "
            f"{result.get('manual_review', 0)} требуют проверки"
        )

    def _completed(self, title, result, callback):
        self.reload()
        self.statusBar().showMessage(title + ": завершено")
        if callback is not None:
            callback(result)

    def _finished(self):
        worker = self.worker
        self.worker = None
        self._set_busy(False)
        if worker is not None:
            worker.wait()
            worker.deleteLater()

    def reload(self):
        try:
            tasks = self.service.list_tasks()
            selected_id = self.selected_task.get("id") if self.selected_task else None
            self.video_table.set_tasks(tasks, selected_id)
            self.dashboard.set_stats(self.service.stats())
            self.mode_badge.setText("DRY-RUN · без отправки" if self.service.config.dry_run else "РУЧНАЯ ПУБЛИКАЦИЯ")
            self._load_history()
            self._load_images()
        except Exception as exc:
            self.statusBar().showMessage("Не удалось обновить интерфейс: " + safe_error(exc))

    def select_task(self, task):
        self.selected_task = dict(task)
        self.task_title.setText(f"Видео {task.get('video_id', '')} · задача {task.get('id', '')}")
        self.author_field.setText(task.get("author") or "")
        self.date_field.setText(task.get("published_at") or "")
        self.description_field.setPlainText(task.get("description") or "")
        self.comment_editor.setPlainText(task.get("comment") or "")
        if not task.get("published_at"):
            self.detail_tabs.setCurrentIndex(1)
        elif task.get("comment"):
            self.detail_tabs.setCurrentIndex(0)
        checks = task.get("checklist") or {}
        self.require_image.setChecked(bool(checks.get("require_image", self.service.config.require_image)))
        self.require_like.setChecked(bool(checks.get("require_like", self.service.config.require_like)))
        slot_index = self.required_image_slot.findData(checks.get("required_image_slot"))
        self.required_image_slot.setCurrentIndex(max(0, slot_index))
        for key, control in self.checks.items():
            control.setChecked(bool(checks.get(key, False)))
        self.task_details.setText(
            f"Аккаунт: {task.get('account_id', '—')} · картинка: {task.get('image_slot') or 'нет'} · "
            f"отчёт: {LABELS.get(task.get('report_status'), task.get('report_status', 'нет'))} · "
            f"вознаграждение: {task.get('reward_status', 'не подтверждено')} · "
            f"ожидается: {task.get('expected_reward', 0)} · подтверждено: {task.get('confirmed_reward', 0)}\n"
            f"{task.get('url', '')}\n{task.get('error', '')}"
        )

    def _comment_length(self):
        text = self.comment_editor.toPlainText()
        self.comment_hint.setText(f"{len(text)} / {self.service.config.max_comment_length} символов · RubUP и уникальность проверяются при сохранении")

    def clear_filters(self):
        self.search_field.clear()
        self.status_filter.setCurrentIndex(0)

    def import_urls(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Добавить ссылки TikTok")
        dialog.resize(650, 320)
        layout = QVBoxLayout(dialog)
        note = QLabel("По одной прямой ссылке https://www.tiktok.com/@автор/video/ID в строке. Метаданные можно проверить и дополнить после импорта.")
        note.setWordWrap(True)
        layout.addWidget(note)
        text = QPlainTextEdit()
        text.setObjectName("importUrls")
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted and text.toPlainText().strip():
            value = text.toPlainText()
            self._launch("Импорт ссылок", lambda: self.service.import_urls(value), self._show_import_result)

    def import_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Импорт ссылок и метаданных", "", "Ссылки (*.txt *.csv *.json)")
        if path:
            self._launch("Импорт файла", lambda: self.service.import_file(path), self._show_import_result)

    def _show_import_result(self, result):
        self.statusBar().showMessage("Импорт: " + json.dumps(result, ensure_ascii=False))

    def start_search(self):
        self._periodic.setInterval(max(1, self.service.config.search_interval) * 1000)
        self._periodic.start()
        self.search_stop.setEnabled(True)
        self._launch("Фильтрация импортированных видео", self.service.refresh)

    def _periodic_refresh(self):
        if self.worker is None:
            self._launch("Периодическая фильтрация", self.service.refresh)

    def stop_search(self):
        self._periodic.stop()
        self.search_stop.setEnabled(False)
        self.statusBar().showMessage("Периодический поиск остановлен.")

    def start_processing(self):
        self._launch("Подготовка комментариев", lambda: self.service.prepare_pending(stop_event=self.stop_event, pause_event=self.pause_event, progress=lambda value: self.worker.progress.emit(value)))

    def pause_processing(self):
        self.pause_event.set()
        self.pause_button.setEnabled(False)
        self.resume_button.setEnabled(True)
        self.statusBar().showMessage("Пауза: текущий запрос завершится; новые задачи ожидают.")

    def resume_processing(self):
        self.pause_event.clear()
        self.pause_button.setEnabled(True)
        self.resume_button.setEnabled(False)
        self.statusBar().showMessage("Обработка продолжена.")

    def stop_processing(self):
        self.stop_event.set()
        self.pause_event.clear()
        self.statusBar().showMessage("Остановка после текущего запроса. Состояние сохранится.")

    def enrich_metadata(self):
        try:
            self._task_id()
            task_id = self._task_id()
        except ValueError as exc:
            self._error(str(exc))
            return
        self._launch("Метаданные oEmbed (дата может остаться неизвестной)", lambda: self.service.enrich_metadata(task_id))

    def save_metadata(self):
        self._local(lambda: self.service.update_metadata(
            self._task_id(), self.date_field.text().strip() or None,
            self.description_field.toPlainText().strip(), self.author_field.text().strip()))

    def save_comment(self):
        self._local(lambda: self.service.save_comment(self._task_id(), self.comment_editor.toPlainText()))

    def regenerate(self):
        try:
            task_id = self._task_id()
        except ValueError as exc:
            self._error(str(exc))
            return
        self._launch("Повторная генерация", lambda: self.service.prepare_one(task_id, regenerate=True))

    def copy_comment(self):
        try:
            task_id = self._task_id()
            from validators import ValidationError, validate_comment
            text = validate_comment(
                self.comment_editor.toPlainText(),
                self.service.config.max_comment_length,
                self.service.db.recent_comments(10000, exclude_task_id=task_id),
                advertising=self.service.config.advertising,
                disclosure=self.service.config.disclosure,
            )
            if self.service.db.comment_exists(text, exclude_task_id=task_id):
                raise ValidationError("Этот комментарий уже использован")
            QApplication.clipboard().setText(text)
            self.statusBar().showMessage("Текст проверен и скопирован. Публикация выполняется вами в TikTok.")
        except Exception as exc:
            self._error(safe_error(exc))

    def open_video(self):
        try:
            self._task_id()
            from validators import canonical_tiktok_url
            url, _ = canonical_tiktok_url(self.selected_task["url"])
            QDesktopServices.openUrl(QUrl(url))
        except Exception as exc:
            self._error(safe_error(exc))

    def open_image(self):
        try:
            self._task_id()
            slot = self.selected_task.get("image_slot")
            image = self.service.images.get_image(slot) if slot else None
            if not image:
                raise ValueError("Для этой задачи картинка не выбрана.")
            if not self.service.images.verify(slot):
                raise ValueError("Проверка целостности оригинала не пройдена.")
            expected = (self.selected_task.get("checklist") or {}).get("image_sha256")
            if expected and expected != image.get("sha256"):
                raise ValueError("Оригинал в этом слоте заменён после подготовки задачи. Повторно проверьте требования и подготовьте черновик.")
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(image["path"]).resolve())))
        except Exception as exc:
            self._error(safe_error(exc))

    def save_requirements(self):
        self._local(lambda: self.service.set_task_requirements(
            self._task_id(), self.require_image.isChecked(),
            self.required_image_slot.currentData(), self.require_like.isChecked()))

    def save_checklist(self):
        self._local(lambda: self.service.report_tracker.set_checklist(
            self._task_id(), {key: control.isChecked() for key, control in self.checks.items()}))

    def confirm_publication(self):
        def confirm():
            task_id = self._task_id()
            # The stored, validated draft must match what the user confirms.
            if self.comment_editor.toPlainText().strip() != (self.selected_task.get("comment") or "").strip():
                raise ValueError("Сначала проверьте и сохраните изменённый комментарий.")
            return self.service.confirm_publication(task_id, {key: control.isChecked() for key, control in self.checks.items()})
        self._local(confirm)

    def confirm_report_submitted(self):
        self._confirm_report("Подтверждаете, что отчёт действительно подан?", "report_submitted")

    def confirm_report_accepted(self):
        self._confirm_report("Подтверждаете, что получили отдельное подтверждение принятия отчёта?", "report_accepted")

    def _confirm_report(self, prompt, method):
        if not self.selected_task:
            self._error("Сначала выберите задачу.")
            return
        if QMessageBox.question(self, "Подтверждение факта", prompt) == QMessageBox.StandardButton.Yes:
            self._local(lambda: getattr(self.service.report_tracker, method)(self._task_id()))

    def confirm_reward(self):
        if not self.selected_task:
            self._error("Сначала выберите задачу.")
            return
        amount, ok = QInputDialog.getDouble(self, "Подтверждённое начисление", "Фактически начисленная сумма (в валюте задания):", 0, 0, 1000000, 4)
        if ok:
            self._local(lambda: self.service.report_tracker.confirm_reward(self._task_id(), amount))

    def choose_image(self, slot):
        path, _ = QFileDialog.getOpenFileName(self, f"Оригинал {slot}", "", "Изображения (*.jpg *.jpeg *.png)")
        if path:
            self._local(lambda: self.service.images.set_image(slot, path))

    def _load_images(self):
        for slot, preview in self.image_previews.items():
            try:
                image = self.service.images.get_image(slot)
                if image:
                    pixmap = QPixmap(str(image["path"]))
                    preview.setPixmap(pixmap.scaled(330, 170, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                    valid = self.service.images.verify(slot)
                    self.image_descriptions[slot].setText(f"{image.get('width', '?')} × {image.get('height', '?')} · " + ("целостность OK" if valid else "ФАЙЛ ИЗМЕНЁН") + f"\nSHA-256: {image.get('sha256', '')[:20]}…")
                else:
                    preview.clear()
                    preview.setText("Файл не добавлен")
                    self.image_descriptions[slot].setText("Ожидается ваш оригинал")
            except Exception as exc:
                self.image_descriptions[slot].setText(safe_error(exc))

    def _load_history(self):
        rows = self.service.db.events()
        lines = []
        for row in rows[-500:]:
            lines.append(f"{row.get('created_at', row.get('timestamp', ''))} | задача {row.get('task_id', '—')} | {row.get('kind', row.get('event', ''))} | {row.get('detail', '')}")
        self.history.setPlainText("\n".join(lines) if lines else "Событий пока нет. История сохраняется локально в SQLite.")
        costs = getattr(self.service, "costs", None)
        if costs is not None:
            estimate = costs.estimate(count=10000, input_tokens=250, output_tokens=40)
            summary = costs.summary()
            def money(value):
                return "неизвестно — задайте тарифы" if value is None else f"${value:,.4f}"
            self.cost_summary.setText(
                "10 000 запросов · по 250 входных и 40 выходных токенов · предварительная оценка\n"
                f"OpenAI: {money(estimate.get('openai_cost'))} · источник TikTok: {money(estimate.get('third_party_cost'))} · "
                f"всего: {money(estimate.get('total_cost'))}\n"
                f"Фактически: {summary.get('requests', 0)} запросов · вход: {summary.get('input_tokens', 0)} токенов · "
                f"выход: {summary.get('output_tokens', 0)} токенов · расходы: {money(summary.get('total_cost'))}\n"
                f"Ожидаемое вознаграждение: {summary.get('estimated_reward', 0)} · "
                f"подтверждённое начисление: {summary.get('confirmed_reward', 0)}\n"
                "Тарифы задаются вручную. Повторные запросы увеличивают расходы; интернет и электричество учитываются отдельно."
            )

    def export_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "Экспорт истории", "rubup-history.csv", "CSV (*.csv)")
        if path:
            self._local(lambda: self.service.export_csv(path))
            self.statusBar().showMessage("Экспорт: " + path)

    def open_settings(self):
        if SettingsWindow(self.service, self).exec() == QDialog.DialogCode.Accepted:
            self.stop_search()
            self.reload()
            QMessageBox.information(self, "Настройки сохранены", "Настройки записаны в config.yaml. Перезапустите программу, чтобы применить изменения.")

    def send_links(self):
        if self.service.config.dry_run:
            self._error("В dry-run реальные сообщения Telegram запрещены.")
            return
        if self.service.config.telegram_mode == "disabled":
            self._error("Сначала выберите режим Telegram в настройках.")
            return
        prompt = "Отправить реальные прямые ссылки в Telegram для задач, достигших выбранного статуса? Ранее отправленные ссылки повторяться не будут."
        if QMessageBox.question(self, "Отправка Telegram", prompt) == QMessageBox.StandardButton.Yes:
            self._launch("Отправка ссылок", self.service.send_links)

    def run_dry(self):
        self._launch("Изолированный dry-run", self.service.dry_run, self._dry_result)

    def _dry_result(self, result):
        dialog = QDialog(self)
        dialog.setWindowTitle("Dry-run — тестовые данные, реальные отправки отключены")
        dialog.resize(720, 440)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        text.setReadOnly(True)
        layout.addWidget(text)
        button = QPushButton("Закрыть")
        button.clicked.connect(dialog.accept)
        layout.addWidget(button)
        dialog.exec()

    def closeEvent(self, event):
        self._periodic.stop()
        if self.worker is not None and self.worker.isRunning():
            self.stop_event.set()
            self.pause_event.clear()
            # Never destroy a running QThread. The API timeout bounds each request.
            event.ignore()
            self.statusBar().showMessage("Завершение текущего запроса… Окно закроется после безопасной остановки.")
            self.worker.finished.connect(self.close)
            return
        self.service.close()
        event.accept()
