"""Five clear pages over the existing durable task workflow."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import re
import threading

from PySide6.QtCore import QThread, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QScrollArea, QStackedWidget, QTabWidget, QTextEdit,
    QToolButton, QVBoxLayout, QWidget,
)

from gui.dashboard import Dashboard
from gui.image_page import ImagePage
from gui.result_card import ResultCard
from gui.task_cards import TaskCards, status_label
from gui.theme import STYLE
from gui.video_table import LABELS, VideoTable


def safe_error(exc):
    """Only useful local messages; never display network tracebacks or secrets."""
    text = str(exc)
    if 'OPENAI_API_KEY' in text:
        return 'Добавьте ключ OpenAI в разделе «Настройки → Искусственный интеллект».'
    if 'TELEGRAM_BOT_TOKEN' in text:
        return 'Добавьте токен бота в разделе «Настройки → Telegram».'
    if 'TELEGRAM_CHAT_ID' in text:
        return 'Укажите номер чата в разделе «Настройки → Telegram».'
    if 'Invalid isoformat' in text:
        return 'Укажите дату в формате ГГГГ-ММ-ДД, например 2026-06-12.'
    if 'HTTP 401' in text:
        return 'Ключ не принят сервисом. Проверьте его в настройках.'
    if 'HTTP 403' in text:
        return 'Сервис не разрешил действие. Проверьте права, доступность модели и подключение.'
    text = re.sub(r'/bot[^/\s]+', '/bot[СКРЫТО]', text)
    text = re.sub(r'\bsk-[\w-]+', '[СКРЫТО]', text)
    for word in ('Traceback (most recent call last)', 'httpx.', 'ConnectionError', 'ReadTimeout'):
        if word in text:
            return 'Не удалось подключиться. Проверьте интернет и повторите действие.'
    return text[:1200] or 'Не удалось завершить действие. Попробуйте ещё раз.'


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
        self._closing = False
        self._mutation_controls = []
        self._wizard = None
        self._periodic = QTimer(self)
        self._periodic.timeout.connect(self._periodic_refresh)
        self.setWindowTitle('RubUP Automation')
        self.resize(1280, 900)
        self.setMinimumSize(980, 720)
        self.setStyleSheet(STYLE)
        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        sidebar = QWidget()
        sidebar.setObjectName('sidebar')
        sidebar.setFixedWidth(210)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 28, 18, 18)
        side.setSpacing(8)
        brand = QLabel('RubUP')
        brand.setObjectName('brand')
        side.addWidget(brand)
        version = QLabel('Помощник для TikTok')
        version.setObjectName('muted')
        side.addWidget(version)
        side.addSpacing(26)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons = []
        for i, name in enumerate(('Главная', 'Мои задания', 'Картинки', 'Настройки', 'Помощь')):
            button = QPushButton(name)
            button.setObjectName('nav')
            button.setCheckable(True)
            self.nav_group.addButton(button, i)
            self.nav_buttons.append(button)
            side.addWidget(button)
        self.nav_buttons[0].setChecked(True)
        self.nav_group.idClicked.connect(self.navigate)
        side.addStretch()
        self.mode_badge = QLabel('Публикация вручную')
        self.mode_badge.setObjectName('muted')
        self.mode_badge.setWordWrap(True)
        side.addWidget(self.mode_badge)
        about = QLabel('Версия 2.0\nВаши данные — на компьютере')
        about.setObjectName('tiny')
        side.addWidget(about)
        outer.addWidget(sidebar)
        body = QWidget()
        column = QVBoxLayout(body)
        column.setContentsMargins(28, 24, 28, 16)
        column.setSpacing(14)
        self.demo_banner = QLabel('Демонстрационный режим — реальные действия не выполняются')
        self.demo_banner.setObjectName('demoBanner')
        self.demo_banner.setWordWrap(True)
        self.demo_banner.hide()
        column.addWidget(self.demo_banner)
        self.error_label = QLabel()
        self.error_label.setObjectName('errorMessage')
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        column.addWidget(self.error_label)
        self.pages = QStackedWidget()
        column.addWidget(self.pages, 1)
        self._build_home()
        self._build_tasks()
        self._build_images()
        self._build_settings()
        self._build_help()
        progress_row = QHBoxLayout()
        self.progress_label = QLabel('Готово к работе')
        self.progress_label.setObjectName('muted')
        progress_row.addWidget(self.progress_label, 1)
        self.cancel_button = QPushButton('Отменить')
        self.cancel_button.setObjectName('danger')
        self.cancel_button.clicked.connect(self.stop_processing)
        self.cancel_button.hide()
        progress_row.addWidget(self.cancel_button)
        column.addLayout(progress_row)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.hide()
        column.addWidget(self.progress_bar)
        outer.addWidget(body, 1)
        self._build_details_dialog()
        self.reload()
        self._set_busy(False)
        QTimer.singleShot(0, self._first_run)

    def _button(self, title, callback, primary=False, mutation=True):
        button = QPushButton(title)
        button.clicked.connect(callback)
        if primary:
            button.setObjectName('primary')
        if mutation:
            self._mutation_controls.append(button)
        return button

    def _page(self):
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 10, 0)
        layout.setSpacing(18)
        area.setWidget(widget)
        self.pages.addWidget(area)
        return widget, layout

    @staticmethod
    def _heading(layout, title, subtitle=''):
        label = QLabel(title)
        label.setObjectName('pageTitle')
        layout.addWidget(label)
        if subtitle:
            label = QLabel(subtitle)
            label.setWordWrap(True)
            label.setObjectName('muted')
            layout.addWidget(label)

    def _build_home(self):
        self.home_page, layout = self._page()
        self._heading(layout, 'RubUP Automation', 'Помощник для заданий TikTok')
        card = QFrame()
        card.setObjectName('card')
        box = QVBoxLayout(card)
        box.setContentsMargins(24, 22, 24, 22)
        box.setSpacing(15)
        title = QLabel('Начнём с видео')
        title.setObjectName('sectionTitle')
        box.addWidget(title)
        self.url_input = QLineEdit()
        self.url_input.setObjectName('urlInput')
        self.url_input.setPlaceholderText('Вставь ссылку на TikTok-видео')
        self.url_input.setClearButtonEnabled(True)
        self.url_input.returnPressed.connect(self.process_url)
        box.addWidget(self.url_input)
        self.process_button = self._button('НАЧАТЬ ОБРАБОТКУ', self.process_url, primary=True)
        self.process_button.setMinimumHeight(48)
        box.addWidget(self.process_button)
        note = QLabel('Подготовим комментарий и покажем результат. Опубликовать его в TikTok нужно самостоятельно.')
        note.setWordWrap(True)
        note.setObjectName('muted')
        box.addWidget(note)
        demo = self._button('Попробовать бесплатно', self.run_dry)
        demo.setObjectName('linkButton')
        box.addWidget(demo, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(card)
        self.dashboard = Dashboard()
        layout.addWidget(self.dashboard)
        self.last_title = QLabel('Последнее задание')
        self.last_title.setObjectName('sectionTitle')
        layout.addWidget(self.last_title)
        self.empty_result = QFrame()
        self.empty_result.setObjectName('card')
        empty_box = QVBoxLayout(self.empty_result)
        empty_box.setContentsMargins(22, 24, 22, 24)
        empty_box.addWidget(QLabel('Здесь появится подготовленный комментарий'))
        empty_note = QLabel('Вставьте ссылку выше или начните с бесплатного примера — ключи для него не нужны.')
        empty_note.setWordWrap(True)
        empty_note.setObjectName('muted')
        empty_box.addWidget(empty_note)
        layout.addWidget(self.empty_result)
        self.result_card = ResultCard()
        self.result_card.hide()
        self.comment_editor = self.result_card.comment_editor
        self.result_card.copy_requested.connect(self.copy_comment)
        self.result_card.regenerate_requested.connect(self.regenerate)
        self.result_card.open_requested.connect(self.open_video)
        self.result_card.published_requested.connect(self.publication_dialog)
        self.result_card.review_requested.connect(self.review_missing)
        self.result_card.details_requested.connect(self.open_details)
        self.result_card.image_requested.connect(self.open_image)
        self.result_card.send_requested.connect(self.send_selected_link)
        layout.addWidget(self.result_card)
        save_row = QHBoxLayout()
        self.save_button = self._button('Проверить и сохранить изменения', self.save_comment)
        self.save_button.setObjectName('linkButton')
        self.save_button.hide()
        save_row.addWidget(self.save_button)
        save_row.addStretch()
        self.comment_hint = QLabel()
        self.comment_hint.setObjectName('tiny')
        save_row.addWidget(self.comment_hint)
        layout.addLayout(save_row)
        self.comment_editor.textChanged.connect(self._comment_changed)
        layout.addStretch()

    def _build_tasks(self):
        self.tasks_page, layout = self._page()
        self._heading(layout, 'Мои задания', 'Продолжите подготовку или посмотрите, что уже сделано')
        row = QHBoxLayout()
        self.search_field = QLineEdit()
        self.search_field.setPlaceholderText('Найти по ссылке, автору или комментарию')
        row.addWidget(self.search_field, 1)
        self.task_filter = QComboBox()
        for title, code in (('Все', 'all'), ('Готовые', 'ready'), ('Ожидают публикации', 'wait'),
                            ('Опубликованные', 'published'), ('Ошибки', 'errors')):
            self.task_filter.addItem(title, code)
        row.addWidget(self.task_filter)
        row.addWidget(self._button('Очистить фильтры', self.clear_filters, mutation=False))
        layout.addLayout(row)
        self.task_cards = TaskCards()
        self.task_cards.open_task.connect(self.open_task)
        self.task_cards.resume_task.connect(self.resume_task)
        self.search_field.textChanged.connect(self.task_cards.set_query)
        self.task_filter.currentIndexChanged.connect(lambda _: self.task_cards.set_filter(self.task_filter.currentData()))
        layout.addWidget(self.task_cards)
        advanced = QToolButton()
        advanced.setText('Импорт, подробный список и отчёты')
        advanced.setCheckable(True)
        advanced.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        advanced.setArrowType(Qt.ArrowType.RightArrow)
        layout.addWidget(advanced, 0, Qt.AlignmentFlag.AlignLeft)
        details = QWidget()
        details.hide()
        advanced.toggled.connect(details.setVisible)
        advanced.toggled.connect(lambda checked: advanced.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow))
        box = QVBoxLayout(details)
        box.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(self._button('Добавить несколько ссылок', self.import_urls))
        row.addWidget(self._button('Импорт файла', self.import_file))
        row.addWidget(self._button('Сохранить историю в таблицу', self.export_csv, mutation=False))
        row.addStretch()
        box.addLayout(row)
        controls = QHBoxLayout()
        self.search_start = self._button('Проверять импорт по расписанию', self.start_search)
        self.search_stop = self._button('Остановить проверку', self.stop_search, mutation=False)
        self.batch_button = self._button('Подготовить очередь', self.start_processing)
        self.pause_button = self._button('Пауза', self.pause_processing, mutation=False)
        self.resume_button = self._button('Продолжить', self.resume_processing, mutation=False)
        self.stop_button = self._button('Остановить', self.stop_processing, mutation=False)
        for button in (self.search_start, self.search_stop, self.batch_button, self.pause_button,
                       self.resume_button, self.stop_button):
            controls.addWidget(button)
        box.addLayout(controls)
        self.status_filter = QComboBox()
        self.status_filter.addItem('Все подробные статусы', '')
        for code in ('NEW', 'ANALYZED', 'DRAFT_READY', 'PENDING_APPROVAL', 'PUBLISHED', 'FAILED', 'SKIPPED'):
            self.status_filter.addItem(LABELS.get(code, 'Проверяется'), code)
        box.addWidget(self.status_filter)
        self.video_table = VideoTable()
        self.video_table.task_selected.connect(self.select_task)
        self.search_field.textChanged.connect(self.video_table.proxy.set_query)
        self.status_filter.currentIndexChanged.connect(lambda _: self.video_table.proxy.set_status(self.status_filter.currentData()))
        box.addWidget(self.video_table)
        self.cost_summary = QLabel()
        self.cost_summary.setWordWrap(True)
        self.cost_summary.setObjectName('muted')
        box.addWidget(self.cost_summary)
        box.addWidget(self.dashboard.details_widget)
        self.history = QTextEdit()
        self.history.setReadOnly(True)
        self.history.setMaximumHeight(240)
        box.addWidget(self.history)
        self.connection_log = QPlainTextEdit()
        self.connection_log.setReadOnly(True)
        self.connection_log.setMaximumHeight(100)
        box.addWidget(self.connection_log)
        row = QHBoxLayout()
        row.addWidget(self._button('Отправить подходящие ссылки в Telegram', self.send_links))
        row.addStretch()
        box.addLayout(row)
        layout.addWidget(details)
        layout.addStretch()

    def _build_images(self):
        self.image_page = ImagePage(self.service)
        self.image_page.changed.connect(self.reload)
        self.image_page.failed.connect(self._error)
        self.pages.addWidget(self.image_page)
        self.image_previews = self.image_page.previews
        self.image_descriptions = self.image_page.descriptions

    def _build_settings(self):
        from gui.preferences import SettingsPanel
        self.settings_panel = SettingsPanel(self.service)
        self.settings_panel.changed.connect(self._settings_changed)
        self.settings_panel.open_wizard.connect(self.open_wizard)
        self.settings_panel.demo_requested.connect(self.run_dry)
        self.pages.addWidget(self.settings_panel)

    def _build_help(self):
        from gui.help_page import HelpPage
        self.help_page = HelpPage()
        self.pages.addWidget(self.help_page)

    def _build_details_dialog(self):
        self.details_dialog = QDialog(self)
        self.details_dialog.setWindowTitle('Сведения и требования задания')
        self.details_dialog.resize(760, 650)
        layout = QVBoxLayout(self.details_dialog)
        self.task_title = QLabel('Выберите задание')
        self.task_title.setObjectName('sectionTitle')
        layout.addWidget(self.task_title)
        self.detail_tabs = QTabWidget()
        layout.addWidget(self.detail_tabs)
        metadata = QWidget()
        form = QFormLayout(metadata)
        note = QLabel('Введите только то, что вы проверили в исходном видео. Неизвестные сведения можно оставить пустыми.')
        note.setWordWrap(True)
        note.setObjectName('muted')
        form.addRow(note)
        self.author_field = QLineEdit()
        self.date_field = QLineEdit()
        self.date_field.setPlaceholderText('ГГГГ-ММ-ДД')
        self.description_field = QPlainTextEdit()
        self.description_field.setMaximumHeight(120)
        form.addRow('Автор', self.author_field)
        form.addRow('Дата публикации', self.date_field)
        form.addRow('Описание и хештеги', self.description_field)
        self.metadata_button = self._button('Сохранить проверенные сведения', self.save_metadata)
        form.addRow(self.metadata_button)
        form.addRow(self._button('Получить доступные сведения из TikTok', self.enrich_metadata))
        self.detail_tabs.addTab(metadata, 'Сведения о видео')
        requirements = QWidget()
        req = QFormLayout(requirements)
        self.require_image = QCheckBox('Для этого задания нужна картинка')
        self.require_like = QCheckBox('Нужен лайк своему комментарию')
        self.required_image_slot = QComboBox()
        self.required_image_slot.addItem('Номер не задан — использовать выбранный режим', None)
        for slot in range(1, 5):
            self.required_image_slot.addItem(f'Картинка №{slot}', slot)
        req.addRow(self.require_image)
        req.addRow('Номер картинки по заданию', self.required_image_slot)
        req.addRow(self.require_like)
        req.addRow(self._button('Сохранить требования', self.save_requirements))
        self.detail_tabs.addTab(requirements, 'Требования')
        checklist = QWidget()
        checks_box = QVBoxLayout(checklist)
        self.checks = {}
        for key, title in self._checklist_items():
            control = QCheckBox(title)
            self.checks[key] = control
            checks_box.addWidget(control)
        checks_box.addWidget(self._button('Сохранить отметки', self.save_checklist))
        checks_box.addWidget(self._button('Подтвердить публикацию', self.confirm_publication, primary=True))
        row = QHBoxLayout()
        row.addWidget(self._button('Отчёт подан', self.confirm_report_submitted))
        row.addWidget(self._button('Отчёт принят', self.confirm_report_accepted))
        row.addWidget(self._button('Начисление подтверждено', self.confirm_reward))
        checks_box.addLayout(row)
        self.task_details = QLabel()
        self.task_details.setWordWrap(True)
        self.task_details.setObjectName('muted')
        checks_box.addWidget(self.task_details)
        checks_box.addStretch()
        self.detail_tabs.addTab(checklist, 'Подтверждения и отчёт')
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText('Закрыть')
        close.rejected.connect(self.details_dialog.reject)
        layout.addWidget(close)

    @staticmethod
    def _checklist_items():
        return [('published', 'Я опубликовал(а) именно сохранённый комментарий'),
                ('comment_present', 'Комментарий с RubUP виден под нужным видео'),
                ('comment_not_deleted', 'Комментарий не удалён — я это проверил(а)'),
                ('image_attached', 'Требуемая картинка прикреплена'),
                ('own_comment_liked', 'Лайк своему комментарию поставлен')]

    def navigate(self, index):
        self.pages.setCurrentIndex(index)
        self.nav_buttons[index].setChecked(True)

    def _first_run(self):
        if self._closing:
            return
        check = getattr(self.service, 'onboarding_complete', None)
        if check and not check():
            self.open_wizard()

    def open_wizard(self):
        from gui.onboarding import SetupWizard
        if self._wizard is not None and self._wizard.isVisible():
            self._wizard.raise_()
            return
        self._wizard = SetupWizard(self.service, self)
        self._wizard.demo_requested.connect(self.run_dry)
        self._wizard.finished.connect(self._wizard_finished)
        self._wizard.open()

    def _wizard_finished(self, *_):
        self.settings_panel.reload()
        self.reload()

    def _settings_changed(self):
        self.stop_search()
        self.reload()
        result = getattr(self.settings_panel, 'last_save_result', {})
        self.progress_label.setText('Настройки сохранены. Для изменения истории перезапустите программу.'
                                    if result.get('restart_required') else 'Настройки сохранены и применены')

    def _task_id(self):
        if not self.selected_task:
            raise ValueError('Сначала откройте задание в разделе «Мои задания».')
        if self.selected_task.get('demo') or self.selected_task.get('is_test'):
            raise ValueError('Это демонстрационный пример. Реальные действия отключены.')
        return self.selected_task['id']

    def _local(self, function):
        try:
            result = function()
            self.error_label.hide()
            self.reload()
            return result
        except Exception as exc:
            self._error(safe_error(exc))
            return None

    def _error(self, text):
        text = safe_error(text)
        self.error_label.setText(text)
        self.error_label.show()
        self.progress_label.setText('Действие не завершено')
        self.statusBar().showMessage(text)
        self.connection_log.appendPlainText(text)

    def _set_busy(self, busy):
        for control in self._mutation_controls:
            control.setEnabled(not busy)
        self.url_input.setEnabled(not busy)
        self.result_card.set_busy(busy)
        self.image_page.set_busy(busy)
        self.settings_panel.setEnabled(not busy)
        self.pause_button.setEnabled(busy and not self.pause_event.is_set())
        self.resume_button.setEnabled(busy and self.pause_event.is_set())
        self.stop_button.setEnabled(busy)
        self.search_stop.setEnabled(self._periodic.isActive())
        self.cancel_button.setVisible(busy)
        self.progress_bar.setVisible(busy)
        for control in (self.author_field, self.date_field, self.description_field,
                        self.require_image, self.require_like, self.required_image_slot):
            control.setEnabled(not busy)
        for control in self.checks.values():
            control.setEnabled(not busy)

    def _launch(self, title, function, callback=None):
        if self.worker is not None or getattr(self.settings_panel, 'busy', False):
            self.progress_label.setText('Дождитесь завершения текущего действия')
            return False
        self.stop_event.clear()
        self.pause_event.clear()
        self.error_label.hide()
        worker = Worker(function, self)
        self.worker = worker
        worker.result_ready.connect(lambda result: self._completed(title, result, callback))
        worker.failed.connect(self._error)
        worker.progress.connect(self._progress)
        worker.finished.connect(self._finished)
        self._set_busy(True)
        self.progress_label.setText(title)
        worker.start()
        return True

    def _emit_progress(self, value):
        worker = self.worker
        if worker is not None:
            worker.progress.emit(value)

    def _progress(self, value):
        if isinstance(value, str):
            self.progress_label.setText(value)
        else:
            self.progress_label.setText(f"Готово комментариев: {value.get('prepared', 0)} · требуют проверки: {value.get('manual_review', 0)}")

    def _completed(self, title, result, callback):
        self.reload()
        if isinstance(result, dict) and result.get('cancelled'):
            self.progress_label.setText('Обработка отменена. Сохранённые данные остались в истории.')
        else:
            self.progress_label.setText('Готово!')
        if callback:
            callback(result)

    def _finished(self):
        worker = self.worker
        self.worker = None
        if worker is not None:
            worker.wait()
            worker.deleteLater()
        self._set_busy(False)
        if self._closing:
            QTimer.singleShot(0, self.close)

    def reload(self):
        try:
            tasks = self.service.list_tasks()
            is_demo = bool(self.selected_task and (self.selected_task.get('demo') or self.selected_task.get('is_test')))
            selected_id = self.selected_task.get('id') if self.selected_task and not is_demo else None
            self.video_table.set_tasks(tasks, selected_id)
            self.task_cards.set_tasks(tasks)
            self.dashboard.set_stats(self.service.stats())
            self.image_page.reload()
            self._load_history()
            if self.selected_task and not is_demo:
                match = next((row for row in tasks if row['id'] == selected_id), None)
                if match:
                    self.select_task(match)
            self.mode_badge.setText('Демонстрационный режим' if self.service.config.dry_run else 'Публикация вручную')
            self.demo_banner.setVisible(is_demo or self.service.config.dry_run)
        except Exception as exc:
            self.statusBar().showMessage('Не удалось обновить экран: ' + safe_error(exc))

    def _view(self, task):
        if task.get('demo'):
            return dict(task)
        factory = getattr(self.service, 'task_view', None)
        if factory:
            return factory(task)
        from video_analyzer import analyze_video
        analysis = analyze_video(task, self.service.config)
        return {**task, 'review': {**analysis, 'missing': ['date'] if not task.get('published_at') else []}}

    def select_task(self, task):
        task = self._view(dict(task))
        self.selected_task = task
        self.result_card.set_task(task, status_label(task))
        self.empty_result.hide()
        self.result_card.show()
        self.task_title.setText('Видео TikTok · ' + str(task.get('video_id', '')))
        self.author_field.setText(task.get('author') or '')
        self.date_field.setText(task.get('published_at') or '')
        self.description_field.setPlainText(task.get('description') or '')
        checks = task.get('checklist') or {}
        self.require_image.setChecked(bool(checks.get('require_image', self.service.config.require_image)))
        self.require_like.setChecked(bool(checks.get('require_like', self.service.config.require_like)))
        index = self.required_image_slot.findData(checks.get('required_image_slot'))
        self.required_image_slot.setCurrentIndex(max(0, index))
        for key, control in self.checks.items():
            control.setChecked(bool(checks.get(key)))
        self.task_details.setText(
            f"Публикация: {'подтверждена' if task.get('publication_status') == 'PUBLISHED' else 'не подтверждена'}\n"
            f"Отчёт: {LABELS.get(task.get('report_status'), 'нет подтверждения')}\n"
            f"Ожидаемое вознаграждение: {task.get('expected_reward', 0)} · подтверждено: {task.get('confirmed_reward', 0)}"
        )
        self.demo_banner.setVisible(bool(task.get('demo') or task.get('is_test') or self.service.config.dry_run))
        self._comment_changed()

    def open_task(self, task):
        self.select_task(task)
        self.navigate(0)
        QTimer.singleShot(0, self._focus_result)

    def _focus_result(self):
        area = self.pages.widget(0)
        if self.result_card.isVisible() and isinstance(area, QScrollArea):
            area.ensureWidgetVisible(self.result_card, 0, 18)

    def resume_task(self, task):
        self.open_task(task)
        if task.get('comment'):
            review = (self.selected_task or {}).get('review') or {}
            if not review.get('eligible'):
                self.review_missing()
            return
        self._start_task_processing(task['url'])

    def _comment_changed(self):
        text = self.comment_editor.toPlainText()
        self.comment_hint.setText(f'{len(text)} / {self.service.config.max_comment_length} символов' if self.selected_task else '')
        can_save = bool(self.selected_task and not self.selected_task.get('demo') and not self.selected_task.get('is_test')
                        and self.selected_task.get('publication_status') != 'PUBLISHED'
                        and text.strip() != (self.selected_task.get('comment') or '').strip())
        self.save_button.setVisible(can_save)

    def process_url(self):
        url = self.url_input.text().strip()
        if not url:
            self._error('Вставьте ссылку на TikTok-видео в поле выше.')
            self.url_input.setFocus()
            return
        self._start_task_processing(url)

    def _start_task_processing(self, url):
        self.navigate(0)
        self._launch('Проверяем ссылку…', lambda: self.service.process_url(
            url, stop_event=self.stop_event, progress=self._emit_progress), self._processed_result)

    def _processed_result(self, task):
        if not task or task.get('cancelled'):
            return
        self.select_task(task)
        self.navigate(0)
        QTimer.singleShot(0, self._focus_result)
        review = task.get('review') or {}
        if review.get('missing') and not task.get('demo'):
            # A compact optional review can be opened after seeing the saved draft.
            self.progress_label.setText('Комментарий готов предварительно. Проверьте недостающие сведения перед публикацией.')

    def clear_filters(self):
        self.search_field.clear()
        self.task_filter.setCurrentIndex(0)
        self.status_filter.setCurrentIndex(0)

    def open_details(self):
        try:
            self._task_id()
        except ValueError as exc:
            self._error(str(exc))
            return
        self.details_dialog.open()

    def review_missing(self):
        try:
            task_id = self._task_id()
            task = self._view(self.service.db.get_task(task_id))
        except Exception as exc:
            self._error(safe_error(exc))
            return
        missing = (task.get('review') or {}).get('missing') or []
        # The review dialog asks only for fields still unavailable. Full edits
        # remain available through the explicit task details action.
        need_date = not task.get('published_at') or any('date' in str(item).lower() or 'дата' in str(item).lower() for item in missing)
        need_topic = not task.get('description') or any('topic' in str(item).lower() or 'тем' in str(item).lower() for item in missing)
        dialog = QDialog(self)
        dialog.setWindowTitle('Проверим недостающие сведения')
        dialog.resize(580, 330)
        box = QVBoxLayout(dialog)
        text = QLabel('Черновик уже сохранён. Откройте исходное видео и дополните только неизвестные сведения.')
        text.setWordWrap(True)
        box.addWidget(text)
        form = QFormLayout()
        date_edit = QLineEdit(task.get('published_at') or '')
        date_edit.setPlaceholderText('ГГГГ-ММ-ДД')
        topic_edit = QPlainTextEdit(task.get('description') or '')
        topic_edit.setPlaceholderText('Описание и #хештеги из видео')
        if need_date:
            form.addRow('Дата публикации', date_edit)
        if need_topic:
            form.addRow('Описание и хештеги', topic_edit)
        if not need_date and not need_topic:
            note = QLabel((task.get('review') or {}).get('reason') or 'Проверьте требования задания в подробностях.')
            note.setWordWrap(True)
            form.addRow(note)
        box.addLayout(form)
        row = QHBoxLayout()
        open_button = QPushButton('Открыть видео')
        open_button.clicked.connect(self.open_video)
        row.addWidget(open_button)
        row.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText('Сохранить проверенные сведения')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('Позже')
        row.addWidget(buttons)
        box.addLayout(row)
        def save():
            result = self._local(lambda: self.service.update_metadata(
                task_id, date_edit.text().strip() if need_date else task.get('published_at'),
                topic_edit.toPlainText().strip() if need_topic else task.get('description', ''), task.get('author', '')))
            if result is not None:
                dialog.accept()
        buttons.accepted.connect(save)
        buttons.rejected.connect(dialog.reject)
        self._review_dialog = dialog
        dialog.open()

    def save_metadata(self):
        self._local(lambda: self.service.update_metadata(
            self._task_id(), self.date_field.text().strip() or None,
            self.description_field.toPlainText().strip(), self.author_field.text().strip()))

    def enrich_metadata(self):
        try:
            task_id = self._task_id()
        except ValueError as exc:
            self._error(str(exc))
            return
        self._launch('Получаем доступные сведения…', lambda: self.service.enrich_metadata(task_id))

    def save_comment(self):
        self._local(lambda: self.service.save_comment(
            self._task_id(), self.comment_editor.toPlainText(), allow_preliminary=True))

    def regenerate(self):
        if self.selected_task and (self.selected_task.get('demo') or self.selected_task.get('is_test')):
            self.run_dry()
            return
        try:
            task_id = self._task_id()
        except ValueError as exc:
            self._error(str(exc))
            return
        if QMessageBox.question(self, 'Новый вариант комментария',
                'Новый запрос OpenAI может расходовать средства. Подготовить другой комментарий?') != QMessageBox.StandardButton.Yes:
            return
        self._launch('Готовим другой комментарий…', lambda: self.service.prepare_one(
            task_id, regenerate=True, allow_preliminary=True), self._processed_result)

    def copy_comment(self):
        try:
            if not self.selected_task:
                raise ValueError('Сначала подготовьте комментарий.')
            from validators import ValidationError, validate_comment
            task = self.selected_task
            task_id = task.get('id')
            demo = bool(task.get('demo') or task.get('is_test'))
            text = validate_comment(self.comment_editor.toPlainText(), self.service.config.max_comment_length,
                () if demo else self.service.db.recent_comments(10000, exclude_task_id=task_id),
                advertising=self.service.config.advertising, disclosure=self.service.config.disclosure)
            if not demo and self.service.db.comment_exists(text, exclude_task_id=task_id):
                raise ValidationError('Этот комментарий уже использован')
            if not demo and text.strip() != (task.get('comment') or '').strip():
                self.service.save_comment(task_id, text, allow_preliminary=True)
                self.reload()
            QApplication.clipboard().setText(text)
            self.progress_label.setText('Комментарий скопирован' + (' · это тестовый пример' if demo else '. Теперь его можно опубликовать вручную.'))
        except Exception as exc:
            self._error(safe_error(exc))

    def open_video(self):
        try:
            self._task_id()
            from validators import canonical_tiktok_url
            url, _ = canonical_tiktok_url(self.selected_task['url'])
            QDesktopServices.openUrl(QUrl(url))
        except Exception as exc:
            self._error(safe_error(exc))

    def open_image(self):
        try:
            self._task_id()
            slot = self.selected_task.get('image_slot')
            image = self.service.images.get_image(slot) if slot else None
            if not image:
                raise ValueError('Для этого задания картинка не требуется.')
            if not self.service.images.verify(slot):
                raise ValueError('Оригинал картинки повреждён. Добавьте его заново.')
            expected = (self.selected_task.get('checklist') or {}).get('image_sha256')
            if expected and expected != image.get('sha256'):
                raise ValueError('Оригинал заменён после подготовки задания. Восстановите прежнюю картинку или заново проверьте требования.')
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(image['path']).resolve())))
        except Exception as exc:
            self._error(safe_error(exc))

    def save_requirements(self):
        self._local(lambda: self.service.set_task_requirements(
            self._task_id(), self.require_image.isChecked(), self.required_image_slot.currentData(),
            self.require_like.isChecked()))

    def save_checklist(self):
        self._local(lambda: self.service.report_tracker.set_checklist(
            self._task_id(), {key: value.isChecked() for key, value in self.checks.items()}))

    def publication_dialog(self):
        try:
            task_id = self._task_id()
            task = self._view(self.service.db.get_task(task_id))
            if not task.get('comment'):
                raise ValueError('Сначала подготовьте комментарий.')
            if not (task.get('review') or {}).get('eligible'):
                self.review_missing()
                return
            if self.comment_editor.toPlainText().strip() != (task.get('comment') or '').strip():
                raise ValueError('Сначала сохраните изменённый комментарий.')
        except Exception as exc:
            self._error(safe_error(exc))
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('Подтвердить ручную публикацию')
        box = QVBoxLayout(dialog)
        note = QLabel('Отметьте только действия, которые вы действительно выполнили в TikTok.')
        note.setWordWrap(True)
        box.addWidget(note)
        checks = {}
        task_checks = task.get('checklist') or {}
        for key, title in self._checklist_items():
            if key == 'image_attached' and not task_checks.get('require_image'):
                continue
            if key == 'own_comment_liked' and not task_checks.get('require_like'):
                continue
            control = QCheckBox(title)
            checks[key] = control
            box.addWidget(control)
        if task.get('image_slot'):
            image_button = QPushButton(f"Показать картинку №{task['image_slot']}")
            image_button.clicked.connect(self.open_image)
            box.addWidget(image_button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('Подтвердить')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('Назад')
        box.addWidget(buttons)
        def confirm():
            try:
                self.service.confirm_publication(task_id, {key: value.isChecked() for key, value in checks.items()})
                dialog.accept()
                self.reload()
                self.progress_label.setText('Ручная публикация подтверждена')
                if QMessageBox.question(self, 'Ссылка для Telegram', 'Отправить прямую ссылку на это видео в Telegram?') == QMessageBox.StandardButton.Yes:
                    self.send_selected_link()
            except Exception as exc:
                self._error(safe_error(exc))
        buttons.accepted.connect(confirm)
        buttons.rejected.connect(dialog.reject)
        self._publication_dialog = dialog
        dialog.open()

    def confirm_publication(self):
        def action():
            task_id = self._task_id()
            if self.comment_editor.toPlainText().strip() != (self.selected_task.get('comment') or '').strip():
                raise ValueError('Сначала сохраните изменённый комментарий.')
            return self.service.confirm_publication(task_id, {key: value.isChecked() for key, value in self.checks.items()})
        self._local(action)

    def send_selected_link(self):
        try:
            task_id = self._task_id()
        except ValueError as exc:
            self._error(str(exc))
            return
        self._launch('Отправляем ссылку в Telegram…', lambda: self.service.send_task_link(task_id),
                     lambda sent: self.progress_label.setText('Ссылка отправлена' if sent else 'Ссылка уже отправлена или текущий этап не допускает отправку'))

    def confirm_report_submitted(self):
        self._confirm_report('Отчёт действительно отправлен?', 'report_submitted')

    def confirm_report_accepted(self):
        self._confirm_report('Вы получили отдельное подтверждение принятия отчёта?', 'report_accepted')

    def _confirm_report(self, prompt, method):
        try:
            task_id = self._task_id()
        except ValueError as exc:
            self._error(str(exc))
            return
        if QMessageBox.question(self, 'Подтверждение факта', prompt) == QMessageBox.StandardButton.Yes:
            self._local(lambda: getattr(self.service.report_tracker, method)(task_id))

    def confirm_reward(self):
        try:
            task_id = self._task_id()
        except ValueError as exc:
            self._error(str(exc))
            return
        amount, accepted = QInputDialog.getDouble(self, 'Подтверждённое начисление',
            'Фактически начисленная сумма в валюте задания:', 0, 0, 1000000, 4)
        if accepted:
            self._local(lambda: self.service.report_tracker.confirm_reward(task_id, amount))

    def import_urls(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('Добавить несколько ссылок')
        dialog.resize(640, 330)
        box = QVBoxLayout(dialog)
        box.addWidget(QLabel('По одной ссылке TikTok в строке'))
        text = QPlainTextEdit()
        box.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('Добавить')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('Отмена')
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        box.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted and text.toPlainText().strip():
            value = text.toPlainText()
            self._launch('Добавляем ссылки…', lambda: self.service.import_urls(value), self._show_import_result)

    def import_file(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Импорт ссылок и сведений', '', 'Списки ссылок (*.txt *.csv *.json)')
        if path:
            self._launch('Импортируем список…', lambda: self.service.import_file(path), self._show_import_result)

    def _show_import_result(self, result):
        self.progress_label.setText(f"Добавлено: {result.get('added', 0)} · уже в истории: {result.get('duplicate', 0)} · отклонено: {result.get('rejected', 0)}")

    def export_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, 'Сохранить историю', 'История RubUP.csv', 'Таблица CSV (*.csv)')
        if path:
            self._local(lambda: self.service.export_csv(path))

    def start_search(self):
        self._periodic.setInterval(max(1, self.service.config.search_interval) * 1000)
        self._periodic.start()
        self.search_stop.setEnabled(True)
        self._launch('Проверяем импортированные видео…', self.service.refresh)

    def _periodic_refresh(self):
        if self.worker is None:
            self._launch('Проверяем импортированные видео…', self.service.refresh)

    def stop_search(self):
        self._periodic.stop()
        self.search_stop.setEnabled(False)

    def start_processing(self):
        self._launch('Готовим комментарии…', lambda: self.service.prepare_pending(
            self.stop_event, self.pause_event, self._emit_progress))

    def pause_processing(self):
        self.pause_event.set()
        self.pause_button.setEnabled(False)
        self.resume_button.setEnabled(True)
        self.progress_label.setText('Пауза после текущего запроса')

    def resume_processing(self):
        self.pause_event.clear()
        self.pause_button.setEnabled(True)
        self.resume_button.setEnabled(False)
        self.progress_label.setText('Продолжаем обработку')

    def stop_processing(self):
        self.stop_event.set()
        self.pause_event.clear()
        self.progress_label.setText('Отмена после завершения текущего запроса…')

    def send_links(self):
        if QMessageBox.question(self, 'Отправка ссылок',
                'Отправить в Telegram подходящие ссылки, достигшие выбранного этапа?') == QMessageBox.StandardButton.Yes:
            self._launch('Отправляем ссылки…', self.service.send_links)

    def run_dry(self):
        self.navigate(0)
        self._launch('Готовим бесплатный пример…', self.service.demo_result, self._demo_result)

    def _demo_result(self, task):
        if not task:
            return
        self.select_task({**task, 'demo': True})
        self.demo_banner.show()
        self.progress_label.setText('Это бесплатный пример. Рабочая история не изменилась.')
        QTimer.singleShot(0, self._focus_result)

    def open_settings(self):
        self.navigate(3)

    def choose_image(self, slot):
        path, _ = QFileDialog.getOpenFileName(self, f'Картинка №{slot}', '', 'Изображения (*.jpg *.jpeg *.png)')
        if path:
            self.image_page.add_image(slot, path)

    def _load_history(self):
        names = {'TASK_UPDATED': 'Задание обновлено', 'DRAFT_READY': 'Комментарий подготовлен',
                 'FAILED': 'Ошибка обработки', 'MANUAL_METADATA': 'Сведения проверены вручную',
                 'DRY_RUN_GENERATION': 'Тестовый комментарий', 'OPENAI_ERROR': 'Ошибка подключения ИИ',
                 'GENERATION_REJECTED': 'Комментарий отправлен на исправление',
                 'MANUAL_PUBLICATION_CONFIRMED': 'Публикация подтверждена пользователем',
                 'REPORT_SUBMITTED': 'Отчёт отправлен', 'REPORT_ACCEPTED': 'Отчёт принят',
                 'REWARD_CONFIRMED': 'Начисление подтверждено', 'TELEGRAM_SENT': 'Ссылка отправлена',
                 'TELEGRAM_UNKNOWN': 'Доставку ссылки нужно проверить', 'TELEGRAM_FAILED': 'Ссылка не отправлена'}
        lines = []
        for row in self.service.db.events()[-300:]:
            label = names.get(row.get('kind'), 'Событие задания')
            lines.append(f"{row.get('created_at', '')} · Задание {row.get('task_id') or '—'} · {label}")
        self.history.setPlainText('\n'.join(lines) if lines else 'История появится после первого задания.')
        costs = getattr(self.service, 'costs', None)
        if costs:
            estimate = costs.estimate(count=10000, input_tokens=250, output_tokens=40)
            summary = costs.summary()
            def money(value):
                return 'неизвестны — задайте тарифы в настройках' if value is None else f'{value:.4f} долл.'
            self.cost_summary.setText(
                f"Расходы ИИ: {money(summary.get('total_cost'))} · Запросов: {summary.get('requests', 0)}\n"
                f"Предварительно на 10 000 запросов: {money(estimate.get('total_cost'))}\n"
                f"Ожидаемое вознаграждение: {summary.get('estimated_reward', 0)} · Подтверждено: {summary.get('confirmed_reward', 0)}"
            )

    def closeEvent(self, event):
        self._periodic.stop()
        if self.worker is not None and self.worker.isRunning():
            self._closing = True
            self.stop_event.set()
            self.pause_event.clear()
            event.ignore()
            self.progress_label.setText('Завершаем текущий запрос и сохраняем результат…')
            return
        if getattr(self.settings_panel, 'busy', False):
            self._closing = True
            event.ignore()
            self.settings_panel.idle.connect(self.close)
            return
        if self._wizard is not None and getattr(self._wizard, 'busy', False):
            self._closing = True
            event.ignore()
            QTimer.singleShot(150, self.close)
            return
        self._closing = True
        if self._wizard is not None:
            self._wizard.reject()
        self.details_dialog.reject()
        self.service.close()
        event.accept()
