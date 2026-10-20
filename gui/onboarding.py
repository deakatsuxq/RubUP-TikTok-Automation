"""Five short setup steps; skipped checks are never displayed as successful."""
from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QVBoxLayout, QWizard, QWizardPage,
)

from gui.preferences import JobRunner, SecretField, friendly_error


class _Step(QWizardPage):
    def __init__(self, title, subtitle, parent=None):
        super().__init__(parent)
        self.setTitle(title)
        self.setSubTitle(subtitle)
        self.layout_box = QVBoxLayout(self)
        self.layout_box.setSpacing(15)


class SetupWizard(QWizard):
    demo_requested = Signal()
    idle = Signal()

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self._closing = False
        self._openai_verified = False
        self._telegram_verified = False
        self._models = []
        self.jobs = JobRunner(self)
        self.jobs.idle.connect(self._became_idle)
        self.jobs.busy_changed.connect(self._busy_changed)
        self.setWindowTitle('Мастер настройки RubUP Automation')
        self.setWizardStyle(QWizard.WizardStyle.ClassicStyle)
        palette = self.palette()
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.AlternateBase):
            palette.setColor(role, QColor('#10151f'))
        palette.setColor(QPalette.ColorRole.WindowText, QColor('#edf1f7'))
        palette.setColor(QPalette.ColorRole.Text, QColor('#edf1f7'))
        self.setPalette(palette)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage)
        self.setButtonText(QWizard.WizardButton.NextButton, 'Далее')
        self.setButtonText(QWizard.WizardButton.BackButton, 'Назад')
        self.setButtonText(QWizard.WizardButton.CancelButton, 'Настроить позже')
        self.setButtonText(QWizard.WizardButton.FinishButton, 'Начать работу')
        self.resize(700, 590)
        self._welcome()
        self._ai()
        self._telegram()
        self._images()
        self._finish()
        credentials = self.service.settings.get_credentials()
        self.openai_key.setText(credentials.get('OPENAI_API_KEY', ''))
        self.telegram_token.setText(credentials.get('TELEGRAM_BOT_TOKEN', ''))
        self.chat_id.setText(credentials.get('TELEGRAM_CHAT_ID', ''))
        self.ai_status.setText('Подключение ещё не проверено. Шаг можно пропустить.')
        self.telegram_status.setText('Бот ещё не проверен. На этом шаге сообщения не отправляются.')
        self.ai_skip.setChecked(not bool(self.openai_key.text()))
        self.telegram_skip.setChecked(not bool(self.telegram_token.text()))

    @property
    def busy(self):
        return self.jobs.busy

    def wait_for_jobs(self):
        self.jobs.wait()

    def _welcome(self):
        page = _Step('Шаг 1. Добро пожаловать', 'Привет! Настроим RubUP Automation за несколько минут.')
        text = QLabel('Вы вставляете ссылку — помощник готовит комментарий.\nВы проверяете результат и публикуете его самостоятельно в TikTok.\n\nПодключения и картинки можно добавить позже. Ваша история не удаляется.')
        text.setWordWrap(True)
        page.layout_box.addWidget(text)
        page.layout_box.addStretch()
        demo = QPushButton('Попробовать бесплатно')
        demo.setObjectName('wizardDemo')
        demo.clicked.connect(self.start_demo)
        page.layout_box.addWidget(demo)
        note = QLabel('Демонстрационный режим — реальные действия не выполняются. Ключи и оплата не нужны.')
        note.setWordWrap(True)
        note.setObjectName('muted')
        page.layout_box.addWidget(note)
        self.addPage(page)

    def _ai(self):
        page = _Step('Шаг 2. Подключение OpenAI', 'Для реальных комментариев нужен ключ API. Подписка ChatGPT не оплачивает API.')
        form = QFormLayout()
        self.openai_key = SecretField('Вставьте ключ OpenAI')
        self.openai_key.edit.setObjectName('wizardOpenaiKey')
        self.openai_key.setToolTip('Ключ сохраняется локально. Не показывайте его другим людям.')
        form.addRow('API-ключ  ?', self.openai_key)
        self.model_combo = QComboBox()
        self.model_combo.addItem(f'{self.service.config.model} — ещё не проверена', self.service.config.model)
        form.addRow('Модель', self.model_combo)
        page.layout_box.addLayout(form)
        row = QHBoxLayout()
        self.ai_check_button = QPushButton('Проверить подключение')
        self.ai_check_button.clicked.connect(self.check_openai)
        row.addWidget(self.ai_check_button)
        key_link = QPushButton('Где взять API-ключ?')
        key_link.clicked.connect(lambda: QDesktopServices.openUrl(QUrl('https://platform.openai.com/api-keys')))
        row.addWidget(key_link)
        page.layout_box.addLayout(row)
        self.ai_status = QLabel()
        self.ai_status.setWordWrap(True)
        page.layout_box.addWidget(self.ai_status)
        self.ai_skip = QCheckBox('Настроить позже — пока без платных запросов')
        page.layout_box.addWidget(self.ai_skip)
        page.layout_box.addStretch()
        self.openai_key.edit.textChanged.connect(self._ai_key_changed)
        self.addPage(page)

    def _telegram(self):
        page = _Step('Шаг 3. Подключение Telegram', 'Необязательно. Бот будет отправлять только выбранные ссылки, по одной на сообщение.')
        form = QFormLayout()
        self.telegram_token = SecretField('Токен от BotFather')
        self.telegram_token.edit.setObjectName('wizardTelegramToken')
        self.chat_id = QLineEdit()
        self.chat_id.setObjectName('wizardChatId')
        self.chat_id.setPlaceholderText('Числовой адрес чата')
        self.chat_id.setToolTip('Узнайте номер своего чата официальным методом getUpdates. Подробности есть в разделе «Помощь».')
        form.addRow('Токен бота', self.telegram_token)
        form.addRow('Chat ID  ?', self.chat_id)
        page.layout_box.addLayout(form)
        self.telegram_check_button = QPushButton('Проверить бота')
        self.telegram_check_button.clicked.connect(self.check_telegram)
        page.layout_box.addWidget(self.telegram_check_button)
        self.telegram_status = QLabel()
        self.telegram_status.setWordWrap(True)
        page.layout_box.addWidget(self.telegram_status)
        guide = QLabel('Создайте бота у @BotFather командой /newbot. Откройте своего бота и нажмите «Начать». Как узнать Chat ID, подробно описано в разделе «Помощь → Telegram». Токен не передавайте сторонним ботам.')
        guide.setWordWrap(True)
        guide.setObjectName('muted')
        page.layout_box.addWidget(guide)
        self.offer_send = QCheckBox('Предлагать отправку после моей публикации')
        self.offer_send.setChecked(True)
        page.layout_box.addWidget(self.offer_send)
        self.telegram_skip = QCheckBox('Подключить Telegram позже')
        page.layout_box.addWidget(self.telegram_skip)
        page.layout_box.addStretch()
        self.telegram_token.edit.textChanged.connect(self._telegram_key_changed)
        self.addPage(page)

    def _images(self):
        page = _Step('Шаг 4. Картинки', 'Добавьте свои четыре оригинала, если они нужны для ваших заданий. Этот шаг можно пропустить.')
        self.image_labels = {}
        for slot in range(1, 5):
            row = QHBoxLayout()
            label = QLabel()
            label.setWordWrap(True)
            self.image_labels[slot] = label
            button = QPushButton('Добавить')
            button.clicked.connect(lambda checked=False, n=slot: self.choose_image(n))
            row.addWidget(label, 1)
            row.addWidget(button)
            page.layout_box.addLayout(row)
        self.image_status = QLabel('JPG, JPEG и PNG сохраняются без сжатия и изменения размеров. Порядок и режим можно выбрать в разделе «Картинки».')
        self.image_status.setWordWrap(True)
        self.image_status.setObjectName('muted')
        page.layout_box.addWidget(self.image_status)
        page.layout_box.addStretch()
        self._refresh_images()
        self.addPage(page)

    def _finish(self):
        page = _Step('Шаг 5. Всё готово', 'Можно начать знакомство с программой. Непроверенные подключения не считаются рабочими.')
        self.finish_summary = QLabel()
        self.finish_summary.setWordWrap(True)
        page.layout_box.addWidget(self.finish_summary)
        self.finish_demo = QCheckBox('Сначала показать бесплатную демонстрацию')
        page.layout_box.addWidget(self.finish_demo)
        page.layout_box.addStretch()
        page.layout_box.addWidget(QLabel('Мастер можно снова открыть в разделе «Настройки».'))
        self.addPage(page)

    def initializePage(self, page_id):
        super().initializePage(page_id)
        if page_id == 4:
            state = self.service.settings.status()
            ai = 'Подключение OpenAI проверено.' if self._openai_verified else ('Ключ OpenAI сохранён, но подключение ещё не проверено.' if state.get('openai_key') else 'OpenAI пока не подключён. Доступна бесплатная демонстрация.')
            tg = 'Бот Telegram ответил; доставка в чат ещё не проверена.' if self._telegram_verified else ('Токен сохранён, но бот ещё не проверен.' if state.get('telegram_token') else 'Telegram можно подключить позже.')
            self.finish_summary.setText(ai + '\n\n' + tg + '\n\nВаша прежняя история и оригиналы сохранены.')
            self.finish_demo.setChecked(not state.get('openai_key'))

    def validateCurrentPage(self):
        if self.busy:
            return False
        try:
            if self.currentId() == 1 and not self.ai_skip.isChecked():
                key = self.openai_key.text()
                if not key:
                    self.ai_status.setText('Вставьте ключ либо выберите «Настроить позже».')
                    return False
                model = self.model_combo.currentData()
                if not model:
                    self.ai_status.setText('Выберите доступную текстовую модель либо настройте подключение позже.')
                    return False
                changes = {'model': model}
                if model != self.service.config.model:
                    changes.update(input_price_per_million=None, output_price_per_million=None)
                config = replace(self.service.config, **changes)
                config._base_dir = self.service.config.base_dir
                self.service.settings.apply(config=config, credentials={'OPENAI_API_KEY': key})
            elif self.currentId() == 2 and not self.telegram_skip.isChecked():
                token, chat_id = self.telegram_token.text(), self.chat_id.text().strip()
                if not token or not chat_id:
                    self.telegram_status.setText('Укажите токен и Chat ID либо подключите Telegram позже.')
                    return False
                config = replace(self.service.config, telegram_mode='published' if self.offer_send.isChecked() else self.service.config.telegram_mode)
                config._base_dir = self.service.config.base_dir
                self.service.settings.apply(config=config, credentials={'TELEGRAM_BOT_TOKEN': token, 'TELEGRAM_CHAT_ID': chat_id})
        except Exception as exc:
            self._show_error(exc)
            return False
        return True

    def _ai_key_changed(self):
        self._openai_verified = False
        self._models = []
        self.model_combo.clear()
        self.model_combo.addItem(f'{self.service.config.model} — ещё не проверена', self.service.config.model)
        self.ai_status.setText('Ключ изменён. Проверка ещё не выполнена.')
        if self.openai_key.text():
            self.ai_skip.setChecked(False)

    def _telegram_key_changed(self):
        self._telegram_verified = False
        self.telegram_status.setText('Токен изменён. Бот ещё не проверен.')
        if self.telegram_token.text():
            self.telegram_skip.setChecked(False)

    def _show_error(self, exc):
        text = friendly_error(exc, (self.openai_key.text(), self.telegram_token.text()))
        (self.telegram_status if self.currentId() == 2 else self.ai_status).setText(text)

    def _busy_changed(self, busy):
        for button in (QWizard.WizardButton.NextButton, QWizard.WizardButton.BackButton,
                       QWizard.WizardButton.FinishButton, QWizard.WizardButton.CancelButton):
            self.button(button).setEnabled(not busy)
        self.ai_check_button.setEnabled(not busy)
        self.telegram_check_button.setEnabled(not busy)
        self.openai_key.setEnabled(not busy)
        self.telegram_token.setEnabled(not busy)
        self.chat_id.setEnabled(not busy)

    def _became_idle(self):
        self.idle.emit()
        if self._closing:
            self._closing = False
            super().reject()

    def check_openai(self):
        key = self.openai_key.text()
        if not key:
            self.ai_status.setText('Сначала вставьте API-ключ.')
            return
        self.ai_status.setText('Проверяем подключение…')
        def success(models):
            self._models = list(models)
            self._openai_verified = bool(models)
            old = self.model_combo.currentData()
            self.model_combo.clear()
            for model in models:
                self.model_combo.addItem(model, model)
            index = self.model_combo.findData(old)
            if index >= 0:
                self.model_combo.setCurrentIndex(index)
            self.ai_status.setText('Ключ принят. Доступные модели получены; выберите одну из списка.' if models else 'Подходящих текстовых моделей в ответе нет. Подключение для генерации не подтверждено.')
        self.jobs.start(lambda: self.service.settings.models(api_key=key), success, self._show_error)

    def check_telegram(self):
        token, chat_id = self.telegram_token.text(), self.chat_id.text().strip()
        self.telegram_status.setText('Проверяем бота, не отправляя сообщений…')
        def success(ok):
            self._telegram_verified = bool(ok)
            self.telegram_status.setText('Бот ответил. Возможность отправки в чат проверяется отдельно.' if ok else 'Бот не подтвердил подключение.')
        self.jobs.start(lambda: self.service.settings.test_telegram(token=token, chat_id=chat_id), success, self._show_error)

    def _refresh_images(self):
        for slot, label in self.image_labels.items():
            image = self.service.images.get_image(slot)
            valid = bool(image and self.service.images.verify(slot))
            label.setText(f'Картинка №{slot} — ' + ('оригинал добавлен' if valid else 'можно добавить позже'))

    def choose_image(self, slot):
        path, _ = QFileDialog.getOpenFileName(self, f'Картинка №{slot}', '', 'Картинки (*.jpg *.jpeg *.png)')
        if not path:
            return
        try:
            self.service.images.set_image(slot, path)
            self._refresh_images()
            self.image_status.setText('Оригинал сохранён без изменений.')
        except Exception as exc:
            self.image_status.setText(friendly_error(exc))

    def accept(self):
        if self.busy:
            return
        try:
            self.service.complete_onboarding()
        except Exception as exc:
            self.finish_summary.setText(friendly_error(exc))
            return
        show_demo = self.finish_demo.isChecked()
        super().accept()
        if show_demo:
            self.demo_requested.emit()

    def start_demo(self):
        if self.busy:
            return
        self.finish_demo.setChecked(True)
        self.accept()

    def reject(self):
        if self.busy:
            self._closing = True
            return
        super().reject()

    def closeEvent(self, event):
        if self.busy:
            self._closing = True
            event.ignore()
        else:
            super().closeEvent(event)
