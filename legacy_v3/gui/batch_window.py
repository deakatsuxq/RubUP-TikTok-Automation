"""RubUP 3.0: persisted batches, two accounts and honest manual confirmations."""
from __future__ import annotations

from pathlib import Path
from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox,
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QScrollArea, QStackedWidget, QTabWidget, QVBoxLayout, QWidget)
import threading

from gui.accounts_page import AccountsPage
from gui.batch_queue import BatchQueue
from gui.help_page import HelpPage
from gui.image_page import ImagePage
from gui.main_window import Worker, safe_error
from gui.preferences import SettingsPanel
from gui.theme import STYLE


class BatchWindow(QMainWindow):
    def __init__(self, service):
        super().__init__()
        self.service, self.batch = service, service.batch
        self.worker, self.current_batch_id, self.demo_data = None, None, None
        self.stop_event, self.pause_event = threading.Event(), threading.Event()
        self._closing, self._wizard, self._legacy = False, None, []
        self.setWindowTitle('RubUP Automation 3.0'); self.resize(1320, 930); self.setMinimumSize(1000, 740); self.setStyleSheet(STYLE)
        central = QWidget(); self.setCentralWidget(central); outer = QHBoxLayout(central); outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(0)
        sidebar = QWidget(); sidebar.setObjectName('sidebar'); sidebar.setFixedWidth(205)
        side = QVBoxLayout(sidebar); side.setContentsMargins(18, 28, 18, 18); side.setSpacing(8)
        brand = QLabel('RubUP'); brand.setObjectName('brand'); side.addWidget(brand)
        tagline = QLabel('Два аккаунта · одна очередь'); tagline.setWordWrap(True); tagline.setObjectName('muted'); side.addWidget(tagline); side.addSpacing(24)
        self.nav_group = QButtonGroup(self); self.nav_group.setExclusive(True); self.nav_buttons = []
        for index, text in enumerate(('Главная', 'Очередь', 'Аккаунты', 'Настройки', 'Помощь')):
            button = QPushButton(text); button.setObjectName('nav'); button.setCheckable(True); side.addWidget(button); self.nav_group.addButton(button, index); self.nav_buttons.append(button)
        self.nav_group.idClicked.connect(self.navigate); side.addStretch()
        note = QLabel('Публикации и лайки — вручную\nВерсия 3.0'); note.setWordWrap(True); note.setObjectName('muted'); side.addWidget(note); outer.addWidget(sidebar)
        body = QWidget(); root = QVBoxLayout(body); root.setContentsMargins(28, 24, 28, 16); root.setSpacing(14)
        self.demo_banner = QLabel('Демонстрационный режим — реальные действия не выполняются'); self.demo_banner.setObjectName('demoBanner'); self.demo_banner.setWordWrap(True); self.demo_banner.hide(); root.addWidget(self.demo_banner)
        self.error_label = QLabel(); self.error_label.setObjectName('errorMessage'); self.error_label.setWordWrap(True); self.error_label.setTextFormat(Qt.TextFormat.PlainText); self.error_label.hide(); root.addWidget(self.error_label)
        self.pages = QStackedWidget(); root.addWidget(self.pages, 1)
        self._build_home()
        self.queue = BatchQueue(service.images); self.queue.action_requested.connect(self.task_action); self.queue.review_requested.connect(self.review_group); self.pages.addWidget(self.queue)
        self.accounts = AccountsPage(self.batch.profiles); self.accounts.failed.connect(self._error); self.accounts.changed.connect(self.reload)
        account_scroll = QScrollArea(); account_scroll.setWidgetResizable(True); account_scroll.setWidget(self.accounts); self.pages.addWidget(account_scroll)
        self._build_settings(); self.help_page = HelpPage(); self.pages.addWidget(self.help_page)
        self.progress_label = QLabel('Готово к работе'); self.progress_label.setObjectName('muted'); self.progress_label.setWordWrap(True); root.addWidget(self.progress_label)
        self.progress_bar = QProgressBar(); self.progress_bar.setRange(0, 0); self.progress_bar.setTextVisible(False); self.progress_bar.hide(); root.addWidget(self.progress_bar)
        outer.addWidget(body, 1)
        for key, slot in (('Ctrl+1', 1), ('Ctrl+2', 2)):
            shortcut = QShortcut(QKeySequence(key), self); shortcut.activated.connect(lambda s=slot: self.copy_current(s))
        next_shortcut = QShortcut(QKeySequence('Ctrl+Right'), self); next_shortcut.activated.connect(self.next_video)
        self.navigate(0); self.reload_batches(restore=True); self._set_busy(False)
        QTimer.singleShot(0, self._first_run)
        QTimer.singleShot(100, self._startup_delivery)

    def _build_home(self):
        page = QWidget(); box = QVBoxLayout(page); box.setContentsMargins(0, 0, 0, 0); box.setSpacing(16)
        title = QLabel('RubUP Automation 3.0'); title.setObjectName('pageTitle'); box.addWidget(title)
        subtitle = QLabel('Вставь TikTok-ссылки'); subtitle.setObjectName('sectionTitle'); box.addWidget(subtitle)
        self.links_input = QPlainTextEdit(); self.links_input.setObjectName('batchLinks'); self.links_input.setPlaceholderText('Вставь ссылки на видео TikTok — по одной на строку\nМожно добавить от 1 до 100 ссылок за один раз'); self.links_input.setMinimumHeight(180); box.addWidget(self.links_input, 1)
        row = QHBoxLayout(); self.import_button = QPushButton('Импорт TXT'); self.import_button.clicked.connect(self.import_txt); row.addWidget(self.import_button)
        self.new_button = QPushButton('Новая очередь'); self.new_button.clicked.connect(self.new_batch); row.addWidget(self.new_button)
        self.batch_choice = QComboBox(); self.batch_choice.setMinimumWidth(240); self.batch_choice.currentIndexChanged.connect(self.choose_batch); row.addWidget(self.batch_choice, 1); box.addLayout(row)
        self.prepare_button = QPushButton('НАЧАТЬ ПОДГОТОВКУ'); self.prepare_button.setObjectName('primary'); self.prepare_button.setMinimumHeight(50); self.prepare_button.clicked.connect(self.start_preparation); box.addWidget(self.prepare_button)
        self.import_summary = QLabel('Один ролик — два разных комментария. Опубликовать их в TikTok нужно самостоятельно.'); self.import_summary.setWordWrap(True); self.import_summary.setObjectName('muted'); box.addWidget(self.import_summary)
        metrics = QHBoxLayout(); self.metrics = {}
        for key, title in [('videos', 'Видео'), ('comments', 'Комментарии готовы'), ('published', 'Публикации подтверждены'), ('telegram', 'Ссылки Telegram')]:
            card = QFrame(); card.setObjectName('metricCard'); layout = QVBoxLayout(card); layout.setContentsMargins(15, 14, 15, 14)
            label = QLabel('0/0'); label.setObjectName('metricValue'); layout.addWidget(label); caption = QLabel(title); caption.setWordWrap(True); caption.setObjectName('muted'); layout.addWidget(caption); metrics.addWidget(card, 1); self.metrics[key] = label
        box.addLayout(metrics)
        controls = QHBoxLayout(); self.pause_button = QPushButton('Пауза'); self.resume_button = QPushButton('Продолжить'); self.stop_button = QPushButton('Остановить')
        self.pause_button.clicked.connect(self.pause); self.resume_button.clicked.connect(self.resume); self.stop_button.clicked.connect(self.stop)
        for button in (self.pause_button, self.resume_button, self.stop_button): controls.addWidget(button)
        controls.addStretch(); self.demo_button = QPushButton('Попробовать бесплатно'); self.demo_button.clicked.connect(self.run_demo); controls.addWidget(self.demo_button); box.addLayout(controls)
        row = QHBoxLayout(); self.queue_button = QPushButton('Перейти к публикациям →'); self.queue_button.clicked.connect(lambda: self.navigate(1)); row.addWidget(self.queue_button)
        self.retry_button = QPushButton('Подготовить только неуспешные'); self.retry_button.clicked.connect(self.retry_failed); row.addWidget(self.retry_button); row.addStretch(); box.addLayout(row)
        note = QLabel('Деньги расходуются только при реальной генерации. Перед стартом программа покажет оценку. Сохранённые комментарии повторно не оплачиваются.'); note.setWordWrap(True); note.setObjectName('muted'); box.addWidget(note)
        area = QScrollArea(); area.setWidgetResizable(True); area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff); area.setWidget(page); self.pages.addWidget(area)

    def _build_settings(self):
        self.settings_tabs = QTabWidget()
        self.settings_panel = SettingsPanel(self.service); self.settings_panel.changed.connect(self._settings_changed); self.settings_panel.open_wizard.connect(self.open_wizard); self.settings_panel.demo_requested.connect(self.run_demo)
        self.settings_tabs.addTab(self.settings_panel, 'Подключения и расходы')
        self.image_page = ImagePage(self.service); self.image_page.failed.connect(self._error); self.image_page.changed.connect(self.reload); self.settings_tabs.addTab(self.image_page, 'Четыре картинки')
        extra = QWidget(); box = QVBoxLayout(extra)
        text = QLabel('Прежняя история, отчёты, CSV и подробные настройки сохранены. Для их просмотра можно открыть отдельное подробное окно версии 2.0.'); text.setWordWrap(True); box.addWidget(text)
        button = QPushButton('Подробная история и одиночный режим'); button.clicked.connect(self.open_legacy); box.addWidget(button); box.addStretch(); self.settings_tabs.addTab(extra, 'Подробная история')
        self.pages.addWidget(self.settings_tabs)

    def navigate(self, index):
        self.pages.setCurrentIndex(index); self.nav_buttons[index].setChecked(True)

    def _first_run(self):
        if not self._closing and not self.service.onboarding_complete(): self.open_wizard()

    def open_wizard(self):
        if self._wizard is not None and self._wizard.isVisible(): self._wizard.raise_(); return
        from gui.onboarding import SetupWizard
        self._wizard = SetupWizard(self.service, self); self._wizard.demo_requested.connect(self.run_demo); self._wizard.finished.connect(lambda _: self._settings_changed()); self._wizard.open()

    def _settings_changed(self):
        self.settings_panel.reload(); self.image_page.reload(); self.reload(); self.progress_label.setText('Настройки сохранены. Новый лимит расходов действует для новых очередей; существующие сохраняют свой лимит.'); QTimer.singleShot(0, self._startup_delivery)

    def _error(self, error):
        self.error_label.setText(safe_error(error)); self.error_label.show(); self.progress_label.setText('Действие не завершено')

    def _set_busy(self, busy):
        for control in (self.prepare_button, self.import_button, self.new_button, self.batch_choice, self.demo_button, self.retry_button): control.setEnabled(not busy)
        if hasattr(self.settings_panel, 'set_demo_mode'): self.settings_panel.set_demo_mode(self.demo_data is not None or self.service.config.dry_run)
        self.links_input.setReadOnly(busy); self.accounts.set_busy(busy); self.queue.set_busy(busy); self.settings_tabs.setEnabled(not busy)
        self.pause_button.setEnabled(busy and not self.pause_event.is_set()); self.resume_button.setEnabled((busy and self.pause_event.is_set()) or (not busy and self.current_batch_id is not None)); self.stop_button.setEnabled(busy); self.progress_bar.setVisible(busy)

    def _launch(self, title, function, callback=None):
        if self.worker is not None or self.settings_panel.busy:
            self.progress_label.setText('Дождитесь завершения текущего действия.'); return False
        self.stop_event.clear(); self.pause_event.clear(); self.error_label.hide()
        self.worker = Worker(function, self); self.worker.result_ready.connect(lambda result: self._completed(result, callback)); self.worker.failed.connect(self._error); self.worker.progress.connect(self._progress); self.worker.finished.connect(self._finished)
        self._set_busy(True); self.progress_label.setText(title); self.worker.start(); return True

    def _completed(self, result, callback):
        if callback: callback(result)

    def _finished(self):
        worker, self.worker = self.worker, None
        if worker is not None: worker.wait(); worker.deleteLater()
        self._set_busy(False)
        if self._closing: QTimer.singleShot(0, self.close)
        if getattr(self, '_prepare_after_import', False):
            self._prepare_after_import = False; QTimer.singleShot(0, self._confirm_and_prepare)
        if getattr(self, '_drain_after_worker', False):
            self._drain_after_worker = False; QTimer.singleShot(0, self.drain)

    def _progress(self, data):
        if isinstance(data, dict): self.update_metrics(data); self.progress_label.setText(data.get('message') or f"Готово комментариев: {data.get('prepared', 0)}")
        else: self.progress_label.setText(str(data))

    def update_metrics(self, summary):
        videos = summary.get('videos_total', 0); comments = summary.get('comments_total', videos * 2)
        self.metrics['videos'].setText(f"{summary.get('videos_ready', 0)}/{videos}")
        self.metrics['comments'].setText(f"{summary.get('comments_ready', 0)}/{comments}")
        self.metrics['published'].setText(f"{summary.get('published_total', 0)}/{comments}")
        self.metrics['telegram'].setText(f"{summary.get('telegram_sent', 0)}/{videos}")

    def reload_batches(self, restore=False):
        batches = self.batch.store.list_batches(); selected = self.current_batch_id
        self.batch_choice.blockSignals(True); self.batch_choice.clear(); self.batch_choice.addItem('Новая очередь', None)
        for item in batches: self.batch_choice.addItem(f"Очередь №{item['id']} · {item.get('created_at', '')[:16]}", item['id'])
        if restore and batches: selected = batches[0]['id']
        self.current_batch_id = selected
        self.batch_choice.setCurrentIndex(max(0, self.batch_choice.findData(selected))); self.batch_choice.blockSignals(False)
        if restore and selected is not None:
            items = self.batch.store.list_items(selected); self.links_input.setPlainText('\n'.join(item['source_url'] for item in items if item['state'] != 'DUPLICATE'))
        self.reload()

    def choose_batch(self):
        self.current_batch_id = self.batch_choice.currentData(); self.demo_data = None; self.demo_banner.hide()
        if self.current_batch_id is not None:
            self.links_input.setPlainText('\n'.join(i['source_url'] for i in self.batch.store.list_items(self.current_batch_id) if i['state'] != 'DUPLICATE'))
        self.reload()

    def new_batch(self):
        self.current_batch_id = None; self.demo_data = None; self.links_input.clear(); self.demo_banner.hide(); self.reload_batches(); self.progress_label.setText('Новая очередь. Предыдущая сохранена и доступна в списке.')

    def reload(self):
        if hasattr(self.settings_panel, 'set_demo_mode'): self.settings_panel.set_demo_mode(self.demo_data is not None or self.service.config.dry_run)
        if self.demo_data is not None:
            self.queue.set_groups(self.demo_data.get('groups') or [], demo=True); self.update_metrics(self.demo_data.get('summary') or {}); return
        self.accounts.reload()
        if self.current_batch_id is None: self.queue.set_groups([]); self.update_metrics({}); return
        self.queue.set_groups(self.batch.groups(self.current_batch_id)); self.update_metrics(self.batch.summary(self.current_batch_id))

    def start_preparation(self):
        text = self.links_input.toPlainText().strip()
        if not text: self._error('Вставьте от 1 до 100 TikTok-ссылок — по одной на строку.'); return
        self.demo_data = None; self.demo_banner.hide()
        self._launch('Принимаем и сохраняем ссылки…', lambda: self.batch.import_links(text, self.current_batch_id), self._imported)

    def import_txt(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Импорт ссылок', '', 'Текстовые файлы (*.txt)')
        if path:
            self.demo_data = None; self.demo_banner.hide(); self._launch('Читаем ссылки…', lambda: self.batch.import_txt(path, self.current_batch_id), self._imported)

    def _imported(self, result):
        self.current_batch_id = result['batch_id']; self.reload_batches()
        self.import_summary.setText(f"Принято: {result.get('accepted', 0)} · дубликатов: {result.get('duplicates', 0)} · некорректных: {result.get('invalid', 0)} · ожидают проверки: {result.get('awaiting_review', 0)}")
        self._prepare_after_import = True

    def _confirm_and_prepare(self, retry=False):
        if self.current_batch_id is None: return
        estimate = self.batch.estimate(self.current_batch_id); requests = estimate.get('requests', 0)
        if not requests:
            self.progress_label.setText('Готовые комментарии уже сохранены. Повторных платных запросов не будет.'); self.navigate(1); return
        cost = estimate.get('total_cost', estimate.get('openai_cost'))
        price = 'Стоимость неизвестна: укажите действующие тарифы модели в настройках.' if cost is None else f'Предварительная стоимость: {cost:.4f} долл.'
        saved_budget = self.batch.summary(self.current_batch_id).get('budget') or {}
        cap = saved_budget.get('limit_usd')
        limit = 'Лимит расходов этой очереди не установлен.' if cap is None else f'Лимит расходов этой очереди: {cap:.4f} долл.'
        if cap != getattr(self.service.config, 'batch_budget_usd', None):
            limit += '\nНовый лимит из настроек действует для новых очередей; здесь сохранён прежний.'
        prompt = f'Нужно подготовить комментариев: {requests}. С исправлениями возможно до {estimate.get("max_requests", requests * 3)} запросов.\n\n{price}\n{limit}\n\nРеальные запросы OpenAI могут расходовать средства. Начать подготовку?'
        if QMessageBox.question(self, 'Стоимость подготовки очереди', prompt) != QMessageBox.StandardButton.Yes:
            self.progress_label.setText('Очередь сохранена. Подготовку можно продолжить позже.'); return
        self._launch('Готовим комментарии для двух аккаунтов…', lambda: self.batch.prepare(self.current_batch_id, stop_event=self.stop_event, pause_event=self.pause_event,
            progress=lambda data: self.worker.progress.emit(data) if self.worker else None, retry_failed=retry), self._prepared)

    def retry_failed(self):
        if self.demo_data is not None: self._error('В демонстрации реальные повторные запросы отключены.'); return
        self._confirm_and_prepare(retry=True)

    def _prepared(self, result):
        self.reload(); self.navigate(1)
        self.progress_label.setText('Подготовка остановлена. Очередь сохранена.' if result.get('stopped') else 'Достигнут лимит расходов. Готовые результаты сохранены.' if result.get('budget_blocked') else f"Подготовка завершена. Комментариев готово: {result.get('prepared', 0)}, ошибок: {result.get('failed', 0)}")
        self._drain_after_worker = True
        if result.get('budget_blocked') and result.get('error'): self._error(result['error'])

    def pause(self):
        self.pause_event.set(); self.pause_button.setEnabled(False); self.resume_button.setEnabled(True); self.progress_label.setText('Пауза после текущего запроса. Очередь сохранится.')

    def resume(self):
        if self.worker is not None:
            self.pause_event.clear(); self.pause_button.setEnabled(True); self.resume_button.setEnabled(False); self.progress_label.setText('Подготовка продолжена.')
        else: self._confirm_and_prepare()

    def stop(self):
        self.stop_event.set(); self.pause_event.clear(); self.progress_label.setText('Остановка после завершения текущего запроса…')

    def run_demo(self):
        self._launch('Готовим бесплатный пример 50 видео / 100 комментариев…', self.batch.demo, self._demo_ready)

    def _demo_ready(self, result):
        self.demo_data = result
        if hasattr(self.settings_panel, 'set_demo_mode'): self.settings_panel.set_demo_mode(True)
        self.demo_banner.show(); self.queue.set_groups(result.get('groups') or [], demo=True); self.update_metrics(result.get('summary') or {}); self.navigate(1); self.progress_label.setText('Тестовые 50 видео и 100 комментариев. Рабочая история не изменилась.')

    def _task(self, task_id):
        groups = (self.demo_data or {}).get('groups') if self.demo_data is not None else self.batch.groups(self.current_batch_id)
        for group in groups or []:
            for task in group.get('tasks') or []:
                if task['id'] == task_id: return task, group
        raise ValueError('Задание не найдено в текущей очереди.')

    def task_action(self, action, task_id):
        try:
            task, group = self._task(task_id)
            if action == 'copy': return self.copy_task(task)
            if self.demo_data is not None or task.get('is_test'): raise ValueError('Это демонстрационный пример. Реальные действия не выполняются.')
            if action == 'open': return self.batch.open_video(task_id)
            if action == 'image': return self.open_image(task)
            if action == 'publish': return self.publication_dialog(task)
            if action == 'like':
                if QMessageBox.question(self, 'Подтвердить лайк', 'Вы действительно поставили лайк своему комментарию в TikTok?') == QMessageBox.StandardButton.Yes:
                    self.batch.confirm_like(task_id); self.reload(); self.drain()
            elif action in ('retry', 'regenerate'):
                if QMessageBox.question(self, 'Подготовка комментария', 'Новый запрос OpenAI может расходовать средства. Продолжить?') != QMessageBox.StandardButton.Yes: return
                operation = self.batch.retry_task if action == 'retry' else self.batch.regenerate
                self._launch('Готовим комментарий…', lambda: operation(task_id), lambda _: self.reload())
            elif action == 'edit': self.edit_comment(task)
            elif action == 'requirements': self.requirements(task)
        except Exception as exc: self._error(exc)

    def copy_task(self, task):
        from validators import validate_comment
        demo = self.demo_data is not None or task.get('is_test')
        text = validate_comment(task.get('comment') or '', max(100, self.service.config.max_comment_length) if demo else self.service.config.max_comment_length,
            () if demo else self.service.db.recent_comments(10000, exclude_task_id=task['id']), advertising=False if demo else self.service.config.advertising, disclosure=self.service.config.disclosure)
        QApplication.clipboard().setText(text); self.progress_label.setText('Тестовый комментарий скопирован' if demo else 'Комментарий скопирован. Проверьте аккаунт в браузере и опубликуйте вручную.')

    def copy_current(self, slot):
        try:
            task = self.queue.current_task(slot)
            if not task: raise ValueError('Для выбранного видео комментарий ещё не подготовлен.')
            self.copy_task(task)
        except Exception as exc: self._error(exc)

    def next_video(self):
        self.navigate(1); self.queue.next_unfinished()

    def publication_dialog(self, task):
        if not (task.get('review') or {}).get('eligible'):
            return self.review_group(self._task(task['id'])[1])
        dialog = QDialog(self); dialog.setWindowTitle('Подтвердить ручную публикацию'); box = QVBoxLayout(dialog)
        note = QLabel('Отметьте только реально выполненные действия. Лайк можно подтвердить отдельной кнопкой после публикации.'); note.setWordWrap(True); box.addWidget(note)
        checks = {}
        for key, text in [('published', 'Я опубликовал этот комментарий с выбранного аккаунта'), ('comment_present', 'Опубликованный текст содержит RubUP'), ('comment_not_deleted', 'Комментарий доступен и не удалён')]:
            widget = QCheckBox(text); box.addWidget(widget); checks[key] = widget
        if (task.get('checklist') or {}).get('require_image'):
            widget = QCheckBox('Требуемый оригинал картинки прикреплён'); checks['image_attached'] = widget; box.addWidget(widget)
            button = QPushButton('Показать нужную картинку'); button.clicked.connect(lambda: self.open_image(task)); box.addWidget(button)
        error = QLabel(); error.setWordWrap(True); error.setObjectName('warning'); box.addWidget(error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel); buttons.button(QDialogButtonBox.StandardButton.Ok).setText('Подтвердить'); buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('Назад'); box.addWidget(buttons)
        def confirm():
            try:
                self.batch.confirm_publication(task['id'], {k: w.isChecked() for k, w in checks.items()}); dialog.accept(); self.reload(); self.drain()
            except Exception as exc: error.setText(safe_error(exc))
        buttons.accepted.connect(confirm); buttons.rejected.connect(dialog.reject); self._publication_dialog = dialog; dialog.open()

    def review_group(self, group):
        if self.demo_data is not None or group.get('is_test'): self._error('Тестовые сведения не используются для реальных заданий.'); return
        if not group.get('video_id'): self._error('Сначала раскройте ссылку запуском подготовки.'); return
        dialog = QDialog(self); dialog.setWindowTitle('Проверить сведения о видео'); dialog.resize(600, 420); box = QVBoxLayout(dialog)
        note = QLabel('Укажите только проверенные сведения из исходного видео. Они общие для двух аккаунтов.'); note.setWordWrap(True); box.addWidget(note)
        form = QFormLayout(); date = QLineEdit(group.get('published_at') or ''); date.setPlaceholderText('ГГГГ-ММ-ДД'); description = QPlainTextEdit(group.get('description') or ''); author = QLineEdit(group.get('author') or '')
        form.addRow('Дата публикации', date); form.addRow('Описание и хештеги', description); form.addRow('Автор', author); box.addLayout(form)
        error = QLabel(); error.setObjectName('warning'); error.setWordWrap(True); box.addWidget(error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel); buttons.button(QDialogButtonBox.StandardButton.Save).setText('Сохранить проверенные сведения'); buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('Позже'); box.addWidget(buttons)
        def save():
            try:
                self.batch.update_metadata(group['video_id'], date.text().strip() or None, description.toPlainText(), author.text().strip()); dialog.accept(); self.reload(); self.drain()
            except Exception as exc: error.setText(safe_error(exc))
        buttons.accepted.connect(save); buttons.rejected.connect(dialog.reject); self._review_dialog = dialog; dialog.open()

    def edit_comment(self, task):
        value, accepted = QInputDialog.getMultiLineText(self, 'Изменить комментарий', 'Проверьте RubUP, маркировку и достоверность текста.', task.get('comment') or '')
        if accepted: self.batch.save_comment(task['id'], value); self.reload()

    def requirements(self, task):
        dialog = QDialog(self); dialog.setWindowTitle('Требования задания'); form = QFormLayout(dialog); checks = task.get('checklist') or {}
        image = QCheckBox('Требуется картинка'); image.setChecked(bool(checks.get('require_image'))); form.addRow(image)
        slot = QComboBox(); slot.addItem('По выбранному режиму', None)
        for number in (1, 2, 3, 4): slot.addItem(f'Картинка №{number}', number)
        slot.setCurrentIndex(max(0, slot.findData(checks.get('required_image_slot')))); form.addRow('Номер картинки', slot)
        like = QCheckBox('Требуется лайк своему комментарию'); like.setChecked(bool(checks.get('require_like'))); form.addRow(like)
        error = QLabel(); error.setWordWrap(True); form.addRow(error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel); form.addRow(buttons)
        def save():
            try:
                self.batch.set_requirements(task['id'], image.isChecked(), slot.currentData(), like.isChecked()); dialog.accept(); self.reload()
            except Exception as exc: error.setText(safe_error(exc))
        buttons.accepted.connect(save); buttons.rejected.connect(dialog.reject); dialog.open()

    def open_image(self, task):
        slot = task.get('image_slot'); image = self.service.images.get_image(slot) if slot else None
        if not image or not self.service.images.verify(slot): raise ValueError('Оригинал картинки отсутствует или повреждён.')
        if (task.get('checklist') or {}).get('image_sha256') != image['sha256']: raise ValueError('Картинка заменена после подготовки. Восстановите прежний оригинал.')
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(image['path']).resolve())))

    def _startup_delivery(self):
        if not self._closing and self.worker is None and (self._wizard is None or not self._wizard.isVisible()): self.drain(all_batches=True)

    def drain(self, all_batches=False):
        if self.demo_data is not None or self.service.config.dry_run: return
        if self.worker is not None: self._drain_after_worker = True; return
        delivery_batch = None if all_batches else self.current_batch_id
        pending = self.batch.store.telegram_queue(delivery_batch)
        if any(item.get('status') in ('PENDING', 'FAILED') for item in pending):
            self._launch('Отправляем подтверждённые ссылки в Telegram…', lambda: self.batch.drain_telegram(delivery_batch), self._delivery_done)

    def _delivery_done(self, result):
        self.reload(); self.progress_label.setText(f"Telegram: отправлено {result.get('sent', 0)}, ошибок {result.get('failed', 0)}, требуют проверки {result.get('unknown', 0)}")

    def open_legacy(self):
        if self.demo_data is not None:
            self._error('Сначала выйдите из демонстрации кнопкой «Новая очередь».'); return
        from application import AppService
        from gui.main_window import MainWindow
        window = MainWindow(AppService(self.service.config_path)); self._legacy.append(window); window.show()

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            self._closing = True; self.stop_event.set(); self.pause_event.clear(); self.progress_label.setText('Завершаем текущий запрос и сохраняем очередь…'); event.ignore(); return
        if self.settings_panel.busy or (self._wizard is not None and self._wizard.busy):
            self._closing = True; QTimer.singleShot(150, self.close); event.ignore(); return
        self._closing = True
        if self._wizard is not None: self._wizard.reject()
        for window in self._legacy: window.close()
        self.service.close(); event.accept()
