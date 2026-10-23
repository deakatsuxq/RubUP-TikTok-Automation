"""Human-facing connection settings, with all HTTP work outside the UI thread."""
from __future__ import annotations

from dataclasses import replace
import re

from PySide6.QtCore import QObject, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox, QFormLayout, QFrame, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QMessageBox, QPushButton, QScrollArea, QTabWidget,
    QVBoxLayout, QWidget,
)


def friendly_error(exc, secrets=()):
    """Only a local, redacted message is displayed; never a traceback."""
    message = str(exc)
    for value in secrets:
        if value:
            message = message.replace(str(value), '[скрыто]')
    message = re.sub(r'\bsk-[\w-]+', '[скрыто]', message)
    message = re.sub(r'(?:/bot)?\d+:[A-Za-z0-9_-]+', '[скрыто]', message)
    if 'OPENAI_API_KEY' in message:
        return 'Добавьте ключ OpenAI в разделе «Настройки → Искусственный интеллект».'
    if 'TELEGRAM_BOT_TOKEN' in message:
        return 'Добавьте токен своего бота в разделе «Настройки → Telegram».'
    if 'TELEGRAM_CHAT_ID' in message:
        return 'Укажите числовой адрес чата в разделе «Настройки → Telegram».'
    if '401' in message:
        return 'Ключ не подошёл. Проверьте, что он скопирован полностью и ещё действует.'
    if '403' in message:
        return 'Доступ запрещён. Проверьте права ключа, доступ к сервису и настройки сети.'
    if '429' in message:
        return 'Сервис ограничил запросы. Проверьте баланс и лимиты, затем попробуйте позже.'
    if type(exc).__name__ not in {'ValueError', 'ValidationError', 'IntegrationError', 'CredentialError'}:
        return 'Не удалось завершить действие. Проверьте подключение и повторите попытку.'
    return message[:700] or 'Действие не выполнено. Проверьте настройки и попробуйте ещё раз.'


class _Job(QThread):
    result = Signal(object)
    error = Signal(object)

    def __init__(self, function, parent=None):
        super().__init__(parent)
        self.function = function

    def run(self):
        try:
            self.result.emit(self.function())
        except Exception as exc:
            self.error.emit(exc)


class JobRunner(QObject):
    """One bounded operation at a time, retained until its native thread exits."""
    idle = Signal()
    busy_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker = None

    @property
    def busy(self):
        return self.worker is not None

    def start(self, function, success, failure):
        if self.busy:
            return False
        worker = _Job(function, self)
        self.worker = worker
        worker.result.connect(success)
        worker.error.connect(failure)
        worker.finished.connect(self._finished)
        self.busy_changed.emit(True)
        worker.start()
        return True

    def _finished(self):
        worker, self.worker = self.worker, None
        if worker is not None:
            worker.wait()
            worker.deleteLater()
        self.busy_changed.emit(False)
        self.idle.emit()

    def wait(self):
        if self.worker is not None:
            self.worker.wait()


class SecretField(QWidget):
    """A password field whose value is revealed only after an explicit click."""
    def __init__(self, placeholder='', parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit()
        self.edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setClearButtonEnabled(True)
        self.toggle = QPushButton('Показать')
        self.toggle.setCheckable(True)
        self.toggle.setToolTip('Показывает ключ только на этом экране. Не делитесь снимком экрана с открытым ключом.')
        self.toggle.toggled.connect(self._toggle)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.toggle)

    def _toggle(self, show):
        self.edit.setEchoMode(QLineEdit.EchoMode.Normal if show else QLineEdit.EchoMode.Password)
        self.toggle.setText('Скрыть' if show else 'Показать')

    def text(self):
        return self.edit.text().strip()

    def setText(self, value):
        self.edit.setText(value or '')


def explained_label(text, explanation):
    label = QLabel(text + '  ?')
    label.setToolTip(explanation)
    return label


def section_title(text):
    label = QLabel(text)
    label.setObjectName('sectionTitle')
    return label


class SettingsPanel(QWidget):
    changed = Signal()
    open_wizard = Signal()
    demo_requested = Signal()
    idle = Signal()

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.last_save_result = {}
        self._available_models = []
        self._demo_mode = False
        self.jobs = JobRunner(self)
        self.jobs.idle.connect(self.idle)
        self.jobs.busy_changed.connect(self._set_busy)
        self._buttons = []
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)
        root.addWidget(section_title('Настройки'))
        intro = QLabel('Подключите помощников один раз. Настройки и история сохранятся после закрытия программы.')
        intro.setWordWrap(True)
        intro.setObjectName('muted')
        root.addWidget(intro)
        tabs = QTabWidget()
        root.addWidget(tabs, 1)
        self._build_ai(self._page(tabs, 'Искусственный интеллект'))
        self._build_telegram(self._page(tabs, 'Telegram'))
        self._build_extra(self._page(tabs, 'Дополнительно'))
        bottom = QHBoxLayout()
        self.save_button = self._button('Сохранить настройки', self.save)
        self.save_button.setObjectName('primary')
        bottom.addWidget(self.save_button)
        bottom.addStretch()
        self.storage_label = QLabel()
        self.storage_label.setObjectName('muted')
        self.storage_label.setWordWrap(True)
        bottom.addWidget(self.storage_label, 1)
        root.addLayout(bottom)
        self.result_label = QLabel('Изменения подключения ещё не проверены.')
        self.result_label.setWordWrap(True)
        self.result_label.setObjectName('muted')
        root.addWidget(self.result_label)
        self.reload()

    @property
    def busy(self):
        return self.jobs.busy

    def wait_for_jobs(self):
        self.jobs.wait()

    def _page(self, tabs, caption):
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        page = QWidget()
        page.setObjectName('detailPane')
        layout = QVBoxLayout(page)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(16)
        area.setWidget(page)
        tabs.addTab(area, caption)
        return layout

    def _button(self, text, callback):
        button = QPushButton(text)
        button.clicked.connect(callback)
        self._buttons.append(button)
        return button

    def _build_ai(self, layout):
        layout.addWidget(section_title('Короткие комментарии с помощью OpenAI'))
        note = QLabel('Подписка ChatGPT не оплачивает запросы этой программы. Расходы списываются с отдельного счёта OpenAI API.')
        note.setWordWrap(True)
        note.setObjectName('muted')
        layout.addWidget(note)
        form = QFormLayout()
        self.openai_key = SecretField('Вставьте свой ключ OpenAI')
        self.openai_key.edit.setObjectName('openaiKey')
        form.addRow(explained_label('API-ключ', 'Ключ разрешает программе обращаться к OpenAI от вашего имени. Он хранится только на компьютере.'), self.openai_key)
        self.model_combo = QComboBox()
        self.model_combo.setObjectName('modelChoice')
        self.model_combo.setToolTip('Список обновляется реальным запросом к OpenAI. Наличие модели в списке не гарантирует доступность каждого её метода.')
        form.addRow(explained_label('Модель', 'Выберите текстовую модель, доступную вашему ключу. Дешевизна зависит от действующего тарифа.'), self.model_combo)
        self.batch_budget = QLineEdit()
        self.batch_budget.setObjectName('batchBudget')
        self.batch_budget.setPlaceholderText('Без лимита — оставьте пустым')
        form.addRow(explained_label('Лимит партии, долл.',
            'Лимит закрепляется при создании новой очереди и сохраняется после перезапуска. Настройка не повышает лимит уже созданной партии. Для ограничения нужны тарифы модели; неизвестный результат останавливает новые запросы.'), self.batch_budget)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        self.check_ai = self._button('Проверить и получить модели', self.check_openai)
        buttons.addWidget(self.check_ai)
        self.economy_button = self._button('Экономичный вариант', self.select_economical)
        buttons.addWidget(self.economy_button)
        buttons.addStretch()
        buttons.addWidget(self._button('Где взять API-ключ?', lambda: QDesktopServices.openUrl(QUrl('https://platform.openai.com/api-keys'))))
        layout.addLayout(buttons)
        self.ai_status = QLabel('Подключение ещё не проверено.')
        self.ai_status.setWordWrap(True)
        layout.addWidget(self.ai_status)
        self.cost_label = QLabel()
        self.cost_label.setWordWrap(True)
        self.cost_label.setObjectName('muted')
        layout.addWidget(self.cost_label)
        layout.addStretch()
        self.openai_key.edit.textChanged.connect(self._ai_changed)
        self.model_combo.currentIndexChanged.connect(self._update_cost)

    def _build_telegram(self, layout):
        layout.addWidget(section_title('Ссылки в ваш Telegram'))
        note = QLabel('Программа отправляет одну прямую ссылку на видео в одном сообщении. Без комментариев, отчётов и дополнительного текста.')
        note.setObjectName('muted')
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.telegram_token = SecretField('Токен, который выдал BotFather')
        self.telegram_token.edit.setObjectName('telegramToken')
        self.chat_id = QLineEdit()
        self.chat_id.setObjectName('telegramChatId')
        self.chat_id.setPlaceholderText('Номер вашего чата')
        form.addRow(explained_label('Токен бота', 'Создайте бота у @BotFather. Токен даёт доступ к боту, поэтому не публикуйте его.'), self.telegram_token)
        form.addRow(explained_label('Chat ID', 'Числовой адрес чата. У группы он обычно начинается со знака минус.'), self.chat_id)
        self.telegram_mode = QComboBox()
        for text, mode in [('Не отправлять', 'disabled'), ('После проверки видео', 'verified'),
                           ('После подготовки комментария', 'draft'), ('После моей публикации', 'published')]:
            self.telegram_mode.addItem(text, mode)
        form.addRow(explained_label('Когда можно отправить', 'Выбранный этап ограничивает отправку. Сообщение отправляется только вашим явным действием.'), self.telegram_mode)
        self.batch_telegram_mode = QComboBox()
        for text, mode in [('После выполнения обоими аккаунтами', 'both'),
                           ('После выполнения одним аккаунтом', 'any'),
                           ('После подготовки обоих комментариев', 'draft'),
                           ('После проверки видео', 'verified'), ('Не отправлять автоматически', 'disabled')]:
            self.batch_telegram_mode.addItem(text, mode)
        form.addRow(explained_label('Пакетная обработка 3.0',
            'После ручных подтверждений ссылка автоматически попадает в очередь Telegram. По умолчанию нужны публикации и обязательные лайки обоих аккаунтов. Один адрес отправляется один раз.'), self.batch_telegram_mode)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        self.check_bot = self._button('Проверить бота', self.check_telegram)
        buttons.addWidget(self.check_bot)
        self.send_test = self._button('Отправить тестовую ссылку', self.send_test_link)
        buttons.addWidget(self.send_test)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.telegram_status = QLabel('Бот ещё не проверен. Проверка не отправляет сообщений.')
        self.telegram_status.setWordWrap(True)
        layout.addWidget(self.telegram_status)
        instruction = QLabel(
            '<b>Как подключить</b><br>'
            '1. Найдите в Telegram <b>@BotFather</b> с отметкой подлинности и отправьте <b>/newbot</b>.<br>'
            '2. Задайте имя и имя пользователя бота. Скопируйте выданный токен в поле выше.<br>'
            '3. Откройте своего бота и нажмите <b>Начать</b>, затем отправьте ему сообщение.<br>'
            '4. Получите числовой Chat ID через официальный метод <b>getUpdates</b> '
            '(подробные шаги есть в разделе «Помощь»). Не передавайте токен посторонним ботам.<br>'
            '5. Сохраните настройки и проверьте бота. Для проверки доставки используйте реальную '
            'проверенную ссылку из своих заданий.'
        )
        instruction.setWordWrap(True)
        instruction.setObjectName('muted')
        instruction.setTextInteractionFlags(instruction.textInteractionFlags())
        layout.addWidget(instruction)
        layout.addStretch()
        self.telegram_token.edit.textChanged.connect(lambda: self.telegram_status.setText('Токен изменён. Проверьте бота ещё раз.'))
        self.chat_id.textChanged.connect(lambda: self.telegram_status.setText('Проверка бота не подтверждает возможность писать в этот чат.'))

    def _build_extra(self, layout):
        layout.addWidget(section_title('Помощь с настройкой'))
        layout.addWidget(self._button('Открыть мастер настройки', self.open_wizard.emit))
        layout.addWidget(self._button('Попробовать бесплатно', self.demo_requested.emit))
        note = QLabel('Демонстрация работает без ключей и не меняет вашу историю. Тестовые задания нельзя отправить в Telegram или выдать за реальные публикации.')
        note.setWordWrap(True)
        note.setObjectName('muted')
        layout.addWidget(note)
        layout.addSpacing(16)
        layout.addWidget(section_title('Расширенные параметры'))
        extra = QLabel('Здесь сохранены фильтры хештегов и дат, ограничения, стоимость, учёт аккаунта, режимы картинок и остальные возможности предыдущей версии.')
        extra.setWordWrap(True)
        extra.setObjectName('muted')
        layout.addWidget(extra)
        layout.addWidget(self._button('Открыть расширенные настройки', self.open_advanced))
        layout.addStretch()

    def _credentials(self):
        return {'OPENAI_API_KEY': self.openai_key.text(),
                'TELEGRAM_BOT_TOKEN': self.telegram_token.text(),
                'TELEGRAM_CHAT_ID': self.chat_id.text().strip()}

    def _set_busy(self, busy):
        for button in self._buttons:
            button.setEnabled(not busy)
        for field in (self.openai_key, self.telegram_token, self.chat_id, self.model_combo, self.telegram_mode,
                      self.batch_budget, self.batch_telegram_mode):
            field.setEnabled(not busy)
        self.send_test.setEnabled(not busy and not self._demo_mode)

    def set_demo_mode(self, active):
        self._demo_mode = bool(active)
        self.send_test.setEnabled(not self.busy and not self._demo_mode)

    def _error(self, exc):
        message = friendly_error(exc, self._credentials().values())
        self.result_label.setText(message)
        return message

    def _ai_changed(self):
        self._available_models = []
        self.model_combo.clear()
        self.model_combo.addItem(f'{self.service.config.model} — ещё не проверена', self.service.config.model)
        self.ai_status.setText('Ключ изменён. Проверьте подключение, чтобы получить доступные модели.')

    def reload(self):
        if self.busy:
            return
        values = self.service.settings.get_credentials()
        for field, name in ((self.openai_key, 'OPENAI_API_KEY'), (self.telegram_token, 'TELEGRAM_BOT_TOKEN')):
            field.edit.blockSignals(True)
            field.setText(values.get(name, ''))
            field.edit.blockSignals(False)
        self.chat_id.blockSignals(True)
        self.chat_id.setText(values.get('TELEGRAM_CHAT_ID', ''))
        self.chat_id.blockSignals(False)
        current = self.service.config.model
        if not self._available_models:
            self.model_combo.clear()
            self.model_combo.addItem(f'{current} — ещё не проверена', current)
        else:
            index = self.model_combo.findData(current)
            if index >= 0:
                self.model_combo.setCurrentIndex(index)
        index = self.telegram_mode.findData(self.service.config.telegram_mode)
        self.telegram_mode.setCurrentIndex(max(0, index))
        index = self.batch_telegram_mode.findData(self.service.config.batch_telegram_mode)
        self.batch_telegram_mode.setCurrentIndex(max(0, index))
        limit = self.service.config.batch_budget_usd
        self.batch_budget.setText('' if limit is None else str(limit))
        status = self.service.settings.status()
        secure = status.get('secure_storage', '')
        protected = 'Windows' in str(secure) or 'DPAPI' in str(secure).upper()
        self.storage_label.setText('Ключи защищены хранилищем Windows.' if protected else 'Ключи хранятся локально. Не передавайте файл настроек с ключами другим людям.')
        self._update_cost()

    def _update_cost(self):
        if not hasattr(self, 'cost_label'):
            return
        selected = self.model_combo.currentData()
        if selected != self.service.config.model:
            self.cost_label.setText('Стоимость новой модели пока неизвестна. Укажите её действующий тариф в расширенных настройках после сохранения.')
            return
        estimate = self.service.costs.estimate(count=1, input_tokens=250, output_tokens=40)
        cost = estimate.get('openai_cost')
        if cost is None:
            self.cost_label.setText('Стоимость пока неизвестна. Тарифы можно указать в расширенных настройках. Название модели само по себе не подтверждает её цену.')
        else:
            self.cost_label.setText(f'Примерно ${cost:.6f} за ответ при 250 входных и 40 выходных токенах. Это оценка по вашему тарифу; повторная генерация оплачивается отдельно.')

    def check_openai(self):
        key = self.openai_key.text()
        if not key:
            self.ai_status.setText('Сначала вставьте API-ключ OpenAI.')
            return
        self.ai_status.setText('Проверяем подключение и доступные модели…')
        def success(models):
            self._available_models = list(models)
            old = self.model_combo.currentData()
            self.model_combo.clear()
            for model in models:
                self.model_combo.addItem(model, model)
            index = self.model_combo.findData(old)
            if index >= 0:
                self.model_combo.setCurrentIndex(index)
            self.ai_status.setText(f'Ключ принят. Доступно текстовых моделей: {len(models)}. Выберите модель и сохраните настройки.' if models else 'Ключ принят, но подходящих текстовых моделей в ответе нет.')
            self._update_cost()
        def failure(exc):
            self.ai_status.setText(self._error(exc))
        self.jobs.start(lambda: self.service.settings.models(api_key=key), success, failure)

    def select_economical(self):
        selected = self.service.settings.suggest_economical(self._available_models)
        if not selected:
            self.ai_status.setText('Сначала получите доступные модели. Экономичный вариант можно выбрать только из ответа OpenAI; тариф всё равно нужно проверить.')
            return
        self.model_combo.setCurrentIndex(self.model_combo.findData(selected))
        self.ai_status.setText('Выбран компактный вариант из доступных моделей. Его текущая цена не проверена — при необходимости укажите тариф.')

    def check_telegram(self):
        token, chat_id = self.telegram_token.text(), self.chat_id.text().strip()
        self.telegram_status.setText('Проверяем бота без отправки сообщений…')
        def success(ok):
            self.telegram_status.setText('Бот ответил. Доставка в выбранный чат ещё не проверена.' if ok else 'Бот не подтвердил подключение.')
        def failure(exc):
            self.telegram_status.setText(self._error(exc))
        self.jobs.start(lambda: self.service.settings.test_telegram(token=token, chat_id=chat_id), success, failure)

    def save(self):
        try:
            selected = self.model_combo.currentData()
            if not selected:
                raise ValueError('Выберите доступную модель или оставьте текущую настройку.')
            limit_text = self.batch_budget.text().strip().replace(',', '.')
            try:
                limit = float(limit_text) if limit_text else None
            except ValueError:
                raise ValueError('Введите сумму лимита числом или оставьте поле пустым') from None
            changes = {'model': selected, 'telegram_mode': self.telegram_mode.currentData(),
                       'batch_telegram_mode': self.batch_telegram_mode.currentData(), 'batch_budget_usd': limit}
            if selected != self.service.config.model:
                changes.update(input_price_per_million=None, output_price_per_million=None)
            config = replace(self.service.config, **changes)
            config._base_dir = self.service.config.base_dir
            self.last_save_result = self.service.settings.apply(config=config, credentials=self._credentials())
            self.result_label.setText('Настройки сохранены. Перезапустите программу для смены истории или аккаунта.' if self.last_save_result.get('restart_required') else 'Настройки сохранены и применены. Проверка подключения выполняется отдельной кнопкой.')
            self.changed.emit()
            self._update_cost()
            return True
        except Exception as exc:
            self._error(exc)
            return False

    def send_test_link(self):
        if self._demo_mode:
            self.telegram_status.setText('В демонстрационном режиме реальные сообщения не отправляются. Сначала вернитесь к рабочей очереди.')
            return
        tasks = [task for task in self.service.list_tasks() if not task.get('is_test')]
        if not tasks:
            self.telegram_status.setText('Сначала обработайте и проверьте реальное видео. Тестовая отправка не использует вымышленные ссылки.')
            return
        value, accepted = QInputDialog.getText(
            self, 'Тестовая отправка ссылки',
            'Вставьте адрес проверенного реального видео из ваших заданий.\nБудет отправлено только одно сообщение с этой ссылкой.',
            text=tasks[0].get('url', ''),
        )
        if not accepted or not value.strip():
            return
        try:
            self.service.settings.apply(credentials={
                'TELEGRAM_BOT_TOKEN': self.telegram_token.text(),
                'TELEGRAM_CHAT_ID': self.chat_id.text().strip(),
            })
        except Exception as exc:
            self.telegram_status.setText(self._error(exc))
            return
        self.telegram_status.setText('Отправляем выбранную ссылку…')
        def success(sent):
            self.telegram_status.setText('Ссылка отправлена. Дополнительного текста в сообщении нет.' if sent else 'Ссылка уже отправлялась или безопасная отправка сейчас недоступна.')
            self.changed.emit()
        def failure(exc):
            self.telegram_status.setText(self._error(exc))
        self.jobs.start(lambda: self.service.send_test_link(value.strip()), success, failure)

    def open_advanced(self):
        from gui.settings_window import SettingsWindow
        dialog = SettingsWindow(self.service, self)
        if dialog.exec() == dialog.DialogCode.Accepted:
            self.reload()
            restart = getattr(dialog, 'save_result', {}).get('restart_required')
            self.result_label.setText('Настройки сохранены. Для смены истории, аккаунта или тестового режима перезапустите программу.' if restart else 'Расширенные настройки сохранены и применены.')
            self.changed.emit()
