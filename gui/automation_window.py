"""RubUP 4.0 desktop: personal comment bank and human-controlled browser work.

This module deliberately uses only the 4.0 service. Browser calls and Telegram
work run outside Qt's GUI thread; publishing and liking remain manual actions.
"""
from __future__ import annotations

import re
import threading

from PySide6.QtCore import QThread, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QScrollArea, QStackedWidget, QTextBrowser,
    QVBoxLayout, QWidget,
)

from gui.theme import STYLE


def safe_message(error):
    """Keep useful service errors and remove credentials from arbitrary errors."""
    value = str(error)
    value = re.sub(r'https?://[^\s]*/bot[^/\s]+[^\s]*', '[адрес Telegram скрыт]', value)
    value = re.sub(r'\b\d{5,}:[A-Za-z0-9_-]{10,}', '[токен скрыт]', value)
    if 'Traceback' in value or 'httpx.' in value:
        return 'Сетевая операция не завершена. Проверьте подключение и повторите вручную.'
    return value[:1400] or 'Действие не завершено. Проверьте настройки и повторите.'


class AutomationWorker(QThread):
    result_ready = Signal(object)
    failed = Signal(str)

    def __init__(self, function, parent=None):
        super().__init__(parent)
        self.function = function

    def run(self):
        try:
            self.result_ready.emit(self.function())
        except Exception as error:
            self.failed.emit(safe_message(error))


class AutomationWindow(QMainWindow):
    def __init__(self, service):
        super().__init__()
        self.service = service
        self.worker = None
        self.stop_event = threading.Event()
        self.current_task = None
        self._closing = self._allow_close = False
        self._worker_result = self._worker_error = None
        self._extra_close_service = None
        self._demo_windows = []
        self.test_mode = bool(getattr(service, 'test_mode', False))
        self.setWindowTitle('RubUP Automation 4.0')
        self.resize(1250, 920)
        self.setMinimumSize(980, 700)
        self.setStyleSheet(STYLE)
        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        sidebar = QWidget()
        sidebar.setObjectName('sidebar')
        sidebar.setFixedWidth(205)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 26, 18, 18)
        brand = QLabel('RubUP')
        brand.setObjectName('brand')
        side.addWidget(brand)
        subtitle = self._label('Ваши комментарии.\nДва отдельных аккаунта.', 'muted')
        side.addWidget(subtitle)
        side.addSpacing(22)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons = []
        for index, name in enumerate(('Главная', 'Мои комментарии', 'Аккаунты', 'Telegram', 'Как пользоваться')):
            button = QPushButton(name)
            button.setObjectName('nav')
            button.setCheckable(True)
            self.nav_group.addButton(button, index)
            self.nav_buttons.append(button)
            side.addWidget(button)
        self.nav_group.idClicked.connect(self.navigate)
        side.addStretch()
        self.demo_button = QPushButton('Тестовый режим')
        self.demo_button.clicked.connect(self.open_demo)
        self.demo_button.setEnabled(not self.test_mode)
        side.addWidget(self.demo_button)
        side.addWidget(self._label('Playwright · Windows 10/11\nПубликации и лайки вручную\nВерсия 4.0', 'muted'))
        outer.addWidget(sidebar)
        body = QWidget()
        box = QVBoxLayout(body)
        box.setContentsMargins(24, 20, 24, 14)
        self.mode_banner = self._label('ТЕСТОВЫЙ РЕЖИМ · отдельная база и профили. Публикаций и отправок в Telegram нет.', 'demoBanner')
        self.mode_banner.setVisible(self.test_mode)
        box.addWidget(self.mode_banner)
        self.error_label = self._label('', 'errorMessage')
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.hide()
        box.addWidget(self.error_label)
        self.pages = QStackedWidget()
        box.addWidget(self.pages, 1)
        self._build_home()
        self._build_bank()
        self._build_accounts()
        self._build_telegram()
        self._build_help()
        self.status_label = self._label('Добавьте свои комментарии, войдите в два аккаунта и вставьте ссылки.', 'muted')
        box.addWidget(self.status_label)
        outer.addWidget(body, 1)
        QShortcut(QKeySequence('Ctrl+1'), self, activated=lambda: self.select_profile_slot(1))
        QShortcut(QKeySequence('Ctrl+2'), self, activated=lambda: self.select_profile_slot(2))
        QShortcut(QKeySequence('Ctrl+Right'), self, activated=self.next_task)
        self.navigate(0)
        self.reload()
        if self.service.stats().get('total'):
            self.status_label.setText('Сохранённая очередь восстановлена. Нажмите «СТАРТ», чтобы продолжить. Подтверждения публикаций и лайков сохранены.')
        QTimer.singleShot(100, self._startup_delivery)

    @staticmethod
    def _label(text, name=None):
        label = QLabel(text)
        label.setWordWrap(True)
        if name:
            label.setObjectName(name)
        return label

    def _page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(12)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        self.pages.addWidget(scroll)
        return page, layout

    def _build_home(self):
        _, box = self._page()
        self.home_area = self.pages.widget(self.pages.count() - 1)
        box.addWidget(self._label('RubUP Automation 4.0', 'pageTitle'))
        box.addWidget(self._label('Вставьте ссылки — откроем видео и подставим ваши комментарии', 'sectionTitle'))
        self.links_input = QPlainTextEdit()
        self.links_input.setObjectName('videoLinks')
        self.links_input.setPlaceholderText('Ссылки TikTok, каждая с новой строки. Например, 50 видео → 100 заданий.\nПолные ссылки и короткие vm.tiktok.com / vt.tiktok.com; до 100 за раз.')
        self.links_input.setMinimumHeight(115)
        self.links_input.setMaximumHeight(170)
        self.links_input.setPlainText(self.service.store.get_setting('gui_links', '') or '')
        box.addWidget(self.links_input)
        controls = QHBoxLayout()
        self.start_button = QPushButton('СТАРТ')
        self.start_button.setObjectName('primary')
        self.start_button.setMinimumHeight(46)
        self.start_button.clicked.connect(self.start)
        self.pause_button = QPushButton('ПАУЗА')
        self.pause_button.clicked.connect(self.pause)
        self.stop_button = QPushButton('СТОП')
        self.stop_button.clicked.connect(self.stop)
        for button in (self.start_button, self.pause_button, self.stop_button):
            controls.addWidget(button)
        box.addLayout(controls)
        self.import_summary = self._label('Подготовленный текст сохраняется. Опубликуйте и поставьте лайк самостоятельно в браузере.', 'muted')
        box.addWidget(self.import_summary)
        metrics = QHBoxLayout()
        self.metrics = {}
        for key, caption in (('remaining', 'Осталось заданий'), ('used_comments', 'Комментариев использовано'), ('completed', 'Действия подтверждены')):
            card = QFrame()
            card.setObjectName('metricCard')
            card_box = QVBoxLayout(card)
            value = self._label('0', 'metricValue')
            card_box.addWidget(value)
            card_box.addWidget(self._label(caption, 'muted'))
            metrics.addWidget(card, 1)
            self.metrics[key] = value
        box.addLayout(metrics)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        box.addWidget(self.progress_bar)
        self.account_stats = self._label('', 'muted')
        box.addWidget(self.account_stats)
        picker = QHBoxLayout()
        self.profile_choice = QComboBox()
        self.profile_choice.currentIndexChanged.connect(self._profile_changed)
        picker.addWidget(self.profile_choice, 1)
        self.task_choice = QComboBox()
        self.task_choice.currentIndexChanged.connect(self._task_changed)
        picker.addWidget(self.task_choice, 2)
        box.addLayout(picker)
        self.task_title = self._label('Текущее задание', 'sectionTitle')
        box.addWidget(self.task_title)
        self.current_url = QLineEdit()
        self.current_url.setReadOnly(True)
        self.current_url.setPlaceholderText('Здесь появится ссылка на выбранный ролик')
        box.addWidget(self.current_url)
        self.comment_text = QPlainTextEdit()
        self.comment_text.setReadOnly(True)
        self.comment_text.setMinimumHeight(80)
        self.comment_text.setMaximumHeight(120)
        self.comment_text.setPlaceholderText('Здесь будет ваш комментарий из банка')
        box.addWidget(self.comment_text)
        self.task_status = self._label('Заданий пока нет.', 'muted')
        box.addWidget(self.task_status)
        actions = QHBoxLayout()
        self.fill_button = QPushButton('Открыть и подставить')
        self.fill_button.clicked.connect(self.open_current)
        self.copy_button = QPushButton('Копировать')
        self.copy_button.clicked.connect(lambda: QApplication.clipboard().setText(self.comment_text.toPlainText()))
        self.publish_button = QPushButton('Я опубликовал')
        self.publish_button.clicked.connect(self.confirm_publication)
        self.like_button = QPushButton('Я поставил лайк')
        self.like_button.clicked.connect(self.confirm_like)
        self.next_button = QPushButton('Следующее →')
        self.next_button.clicked.connect(self.next_task)
        for button in (self.fill_button, self.copy_button, self.next_button):
            actions.addWidget(button)
        box.addLayout(actions)
        confirmations = QHBoxLayout()
        confirmations.addWidget(self.publish_button)
        confirmations.addWidget(self.like_button)
        box.addLayout(confirmations)
        fallback = QHBoxLayout()
        self.manual_fill_button = QPushButton('Я вставил текст вручную')
        self.manual_fill_button.setToolTip('Если TikTok изменил поле: откройте нужное видео и аккаунт, вставьте текст вручную и подтвердите этот шаг.')
        self.manual_fill_button.clicked.connect(self.confirm_manual_fill)
        fallback.addWidget(self.manual_fill_button)
        fallback.addWidget(self._label('Если поле TikTok не найдено, скопируйте и вставьте текст сами.', 'muted'), 1)
        box.addLayout(fallback)
        self.telegram_summary = self._label('', 'muted')
        box.addWidget(self.telegram_summary)
        box.addStretch()

    def _build_bank(self):
        _, box = self._page()
        box.addWidget(self._label('Мои комментарии', 'pageTitle'))
        box.addWidget(self._label('Вставьте 100–500 готовых комментариев, каждый с новой строки. В каждой строке должно быть упоминание RubUP. Используются только ваши тексты.', 'muted'))
        self.bank_summary = self._label('', 'sectionTitle')
        box.addWidget(self.bank_summary)
        self.bank_input = QPlainTextEdit()
        self.bank_input.setPlaceholderText('Какой вариант в RubUP вам нравится?\nИнтересно посмотреть новые предложения RubUP.\nКаждый комментарий — отдельная строка.')
        self.bank_input.setMinimumHeight(200)
        box.addWidget(self.bank_input)
        controls = QHBoxLayout()
        self.bank_add_button = QPushButton('Добавить комментарии')
        self.bank_add_button.setObjectName('primary')
        self.bank_add_button.clicked.connect(self.import_comments)
        controls.addWidget(self.bank_add_button)
        self.bank_reset_button = QPushButton('Сбросить историю использования')
        self.bank_reset_button.clicked.connect(self.reset_usage)
        controls.addWidget(self.bank_reset_button)
        box.addLayout(controls)
        self.bank_feedback = self._label('', 'muted')
        box.addWidget(self.bank_feedback)
        box.addWidget(self._label('Банк сохраняется в SQLite. Одинаковые тексты исключаются. Комментарий случайно назначается только одному заданию и остаётся за ним после перезапуска. Сброс истории не меняет незавершённые задания.', 'muted'))
        box.addWidget(self._label('Сохранённые комментарии', 'sectionTitle'))
        self.bank_list = QPlainTextEdit()
        self.bank_list.setReadOnly(True)
        self.bank_list.setMinimumHeight(150)
        box.addWidget(self.bank_list)

    def _build_accounts(self):
        _, box = self._page()
        box.addWidget(self._label('Два TikTok-аккаунта', 'pageTitle'))
        box.addWidget(self._label('Нажмите «Войти в TikTok» и войдите самостоятельно на официальном сайте. Пароли вводятся только в TikTok. Браузерные сессии сохраняются в разных локальных профилях Playwright.', 'muted'))
        self.account_controls = {}
        for slot in (1, 2):
            card = QFrame()
            card.setObjectName('metricCard')
            row = QVBoxLayout(card)
            row.addWidget(self._label(f'Аккаунт {slot}', 'sectionTitle'))
            name = QLineEdit()
            name.setPlaceholderText('Название или @имя — для вашего удобства')
            row.addWidget(name)
            buttons = QHBoxLayout()
            save = QPushButton('Сохранить название')
            save.clicked.connect(lambda checked=False, s=slot: self.save_profile(s))
            login = QPushButton('Войти в TikTok')
            login.setObjectName('primary')
            login.clicked.connect(lambda checked=False, s=slot: self.open_login(s))
            buttons.addWidget(save)
            buttons.addWidget(login)
            row.addLayout(buttons)
            stats = self._label('', 'muted')
            row.addWidget(stats)
            path = self._label('', 'tiny')
            path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            row.addWidget(path)
            box.addWidget(card)
            self.account_controls[slot] = {'name': name, 'save': save, 'login': login, 'stats': stats, 'path': path}
        box.addWidget(self._label('Приложение не считает наличие профиля подтверждением входа. Проверяйте нужный аккаунт в окне TikTok перед каждой публикацией. CAPTCHA, подтверждение входа и ограничения сайта проходятся вручную; при ограничении приостановите обработку.', 'muted'))
        box.addStretch()

    def _build_telegram(self):
        _, box = self._page()
        box.addWidget(self._label('Telegram', 'pageTitle'))
        box.addWidget(self._label('Когда публикация и лайк подтверждены для обоих аккаунтов, отправляется только прямая ссылка на ролик. Уже отправленные ссылки не повторяются.', 'muted'))
        settings = self.service.telegram_settings()
        form = QFormLayout()
        self.telegram_token = QLineEdit(str(settings.get('token') or ''))
        self.telegram_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.telegram_token.setPlaceholderText('Токен бота от @BotFather')
        self.telegram_chat = QLineEdit(str(settings.get('chat_id') or ''))
        self.telegram_chat.setPlaceholderText('Числовой Chat ID или @имя канала')
        self.telegram_enabled = QCheckBox('Отправлять после подтверждения обоих аккаунтов')
        self.telegram_enabled.setChecked(bool(settings.get('enabled')))
        form.addRow('Токен бота', self.telegram_token)
        form.addRow('Chat ID', self.telegram_chat)
        form.addRow('', self.telegram_enabled)
        box.addLayout(form)
        self.telegram_save = QPushButton('Сохранить Telegram')
        self.telegram_save.setObjectName('primary')
        self.telegram_save.clicked.connect(self.save_telegram)
        box.addWidget(self.telegram_save)
        self.telegram_retry = QPushButton('Проверить очередь отправки')
        self.telegram_retry.clicked.connect(self.drain_telegram)
        box.addWidget(self.telegram_retry)
        self.telegram_status = self._label('', 'sectionTitle')
        box.addWidget(self.telegram_status)
        box.addWidget(self._label('Добавьте бота в нужный чат и дайте право отправлять сообщения. В Windows настройки подключения защищаются DPAPI для текущего пользователя. Если результат отправки неизвестен, ссылка блокируется от повтора — проверьте чат вручную.', 'muted'))
        box.addStretch()

    def _build_help(self):
        help_page = QTextBrowser()
        help_page.setOpenExternalLinks(True)
        help_page.setHtml('''<h1>Как пользоваться RubUP Automation 4.0</h1>
<h2>1. Установка на Windows 10/11</h2>
<p>Распакуйте ZIP в отдельную папку. Установите Python 3.12 (64-bit) с python.org вместе с Python Launcher. Дважды нажмите <b>install.bat</b>: он установит зависимости и Chromium для Playwright. Затем запустите <b>start.bat</b>. Интернет требуется для первой установки.</p>
<h2>2. Подключите два аккаунта</h2>
<p>Откройте «Аккаунты», задайте понятные названия. Нажмите «Войти в TikTok» у первого аккаунта и войдите самостоятельно в официальном окне TikTok. Повторите для второго аккаунта. Пароли приложение не запрашивает. У каждого аккаунта свой постоянный браузерный профиль; он сохраняет вход на вашем компьютере. Убедитесь, что открыты два разных аккаунта. Сессия может истечь — при необходимости войдите снова.</p>
<h2>3. Добавьте свой банк комментариев</h2>
<p>В «Мои комментарии» вставьте готовые тексты: один на строку, до 500 за один импорт. Каждый текст должен упоминать <b>RubUP</b>. Нажмите «Добавить комментарии». Дубликаты и строки без RubUP не добавляются. Для 50 новых видео нужны 100 доступных текстов — по разному комментарию для каждого из двух аккаунтов. Вы сами отвечаете за точность текста и необходимую маркировку рекламы.</p>
<p>Назначение случайное, без повторов. Использованные тексты остаются в истории. «Сбросить историю использования» разрешает повторное использование подходящих завершённых текстов для будущих видео, сохраняя текущие назначения и защиту от одинакового текста у двух аккаунтов одного видео.</p>
<h2>4. Вставьте ссылки и нажмите «СТАРТ»</h2>
<p>На главной странице вставьте ссылки TikTok, каждую с новой строки (до 100 за раз). Полные ссылки проверяются локально, короткие раскрываются через разрешённые перенаправления TikTok. Если короткую ссылку раскрыть не удалось, откройте её сами и вставьте полный адрес видео. Дубликаты не создают новых заданий. Повторный старт сохраняет ранее назначенные комментарии.</p>
<h2>5. Подтвердите действия вручную</h2>
<p>Выберите аккаунт. Playwright откроет его профиль, перейдёт к ролику и заполнит поле вашим текстом. <b>Программа не нажимает кнопку публикации и не ставит лайки.</b> Проверьте аккаунт, видео и текст, самостоятельно нажмите публикацию в TikTok. После успешной публикации нажмите «Я опубликовал» в программе. Поставьте лайк своему комментарию в TikTok и подтвердите «Я поставил лайк». Только ваши подтверждения учитываются в статистике.</p>
<p>После завершения задания программа может открыть следующее и заполнить текст, ожидая нового подтверждения. Для второго аккаунта выберите его очередь. Кнопки Ctrl+1 и Ctrl+2 меняют аккаунт, Ctrl+→ переходит к следующему заданию. Если поле не найдено, войдите в аккаунт, закройте перекрывающее окно или пройдите проверку сайта вручную, затем повторите «Открыть и подставить». При изменении поля TikTok скопируйте и вставьте текст сами и нажмите «Я вставил текст вручную». Это подтверждает только вставку — публикация выполняется и подтверждается отдельно. Неподтверждённое действие не считается выполненным.</p>
<h2>6. Пауза, остановка и восстановление</h2>
<p>«ПАУЗА» отменяет текущий переход к следующему действию; выполненные шаги и назначения остаются. «СТОП» прекращает обработку, сохраняя очередь. Нажмите «СТАРТ», чтобы продолжить. После перезапуска сохранённые задания остаются, но публикации не выводятся из наличия текста в поле. Закрытие программы закрывает её браузерные окна и сохраняет профили. Не открывайте один профиль одновременно из двух копий программы.</p>
<h2>7. Подключите Telegram</h2>
<p>Создайте бота через @BotFather, добавьте его в чат, укажите токен и Chat ID в разделе «Telegram», включите отправку и сохраните. Ссылка отправляется только после подтверждённых публикаций и лайков у обоих аккаунтов. В сообщении будет только URL. При отключённой отправке очередь сохраняется. Кнопка «Проверить очередь отправки» повторяет только безопасные попытки; неизвестные результаты не отправляются повторно.</p>
<p>Чтобы узнать свой Chat ID: откройте чат с созданным ботом в Telegram, нажмите Start и отправьте ему любое сообщение. Затем самостоятельно откройте в браузере официальный адрес <code>https://api.telegram.org/bot&lt;ТОКЕН&gt;/getUpdates</code>, заменив &lt;ТОКЕН&gt; своим токеном. В ответе найдите <code>message.chat.id</code> нужного сообщения. Для группы ID обычно отрицательный; сначала добавьте туда бота и отправьте сообщение в группе. Если <code>result</code> пустой, отправьте боту ещё одно сообщение и обновите страницу. Токен никому не передавайте и не публикуйте скриншот адресной строки. Чтение ответа выполняете вы в браузере; приложение не запрашивает переписку.</p>
<h2>8. Playwright и тестовый режим</h2>
<p>Playwright управляет локальным Chromium. Не удаляйте папки профилей, если хотите сохранить вход. После обновления зависимостей можно повторить install.bat. Смена структуры TikTok, сетевые ограничения, CAPTCHA или завершение сессии могут потребовать ручного действия; программа не обходит защиту сайта. Сетевые ошибки не превращаются в подтверждённые публикации.</p>
<p>Тестовый режим использует отдельную базу и профили: реальные публикации и Telegram запрещены. Журналы находятся в папке logs. Не передавайте другим свою папку data: в ней находятся личные профили и история. Старая база и настройки при обновлении сохраняются; перед расширением базы создаётся резервная копия.</p>''')
        self.pages.addWidget(help_page)

    def navigate(self, index):
        self.pages.setCurrentIndex(index)
        self.nav_buttons[index].setChecked(True)

    def _error(self, error):
        self.error_label.setText(safe_message(error))
        self.error_label.show()
        self.status_label.setText('Действие не завершено. Очередь сохранена.')

    def _busy(self, value):
        enabled = not value and not self._closing
        for control in (self.start_button, self.profile_choice, self.task_choice, self.bank_add_button,
                        self.bank_reset_button, self.telegram_save, self.telegram_retry):
            control.setEnabled(enabled)
        self.demo_button.setEnabled(enabled and not self.test_mode)
        for controls in self.account_controls.values():
            controls['save'].setEnabled(enabled)
            controls['login'].setEnabled(enabled and not self.test_mode)
        self.pause_button.setEnabled(not self._closing)
        self.stop_button.setEnabled(not self._closing)
        self._render_task(enabled=enabled)

    def _launch(self, message, function, callback=None):
        if self.worker is not None or self._closing:
            return False
        self.error_label.hide()
        self.status_label.setText(message)
        self._worker_result = self._worker_error = None
        self._worker_callback = callback
        self.worker = AutomationWorker(function, self)
        self.worker.result_ready.connect(self._result_ready)
        self.worker.failed.connect(self._failed)
        self.worker.finished.connect(self._finished)
        self._busy(True)
        self.worker.start()
        return True

    def _result_ready(self, result):
        self._worker_result = result

    def _failed(self, error):
        self._worker_error = error

    def _finished(self):
        worker = self.worker
        self.worker = None
        if worker:
            worker.deleteLater()
        if self._closing:
            if hasattr(self._worker_result, 'close') and self._worker_result is not self.service:
                self._extra_close_service = self._worker_result
            self._close_in_background()
            return
        result, error, callback = self._worker_result, self._worker_error, self._worker_callback
        self.reload()
        self._busy(False)
        if error:
            if self.stop_event.is_set():
                self.status_label.setText('Обработка приостановлена. Задания и комментарии сохранены.')
            else:
                self._error(error)
        elif callback:
            callback(result)

    def _state(self):
        state = self.service.state()
        return str(state).upper()

    def _task_rows(self):
        profile_id = self.profile_choice.currentData()
        return self.service.tasks(profile_id=profile_id) if profile_id else []

    def reload(self):
        stats = self.service.stats()
        for key, label in self.metrics.items():
            label.setText(str(stats.get(key, 0)))
        total, completed = stats.get('total', 0), stats.get('completed', 0)
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(completed)
        self.account_stats.setText('   ·   '.join(f"Аккаунт {p['slot']}: {p.get('completed', 0)}/{p.get('total', 0)} выполнено" for p in stats.get('profiles', [])))
        status = f"Telegram: отправлено {stats.get('telegram_sent', 0)} · ожидают {stats.get('telegram_pending', 0)} · требуют проверки {stats.get('telegram_unknown', 0)}"
        self.telegram_summary.setText(status)
        self.telegram_status.setText(status)
        profiles = self.service.profiles()
        selected = self.profile_choice.currentData() or self.service.store.get_setting('gui_profile', None)
        self.profile_choice.blockSignals(True)
        self.profile_choice.clear()
        for profile in profiles:
            self.profile_choice.addItem(f"Аккаунт {profile['slot']} · {profile.get('name') or 'без названия'}", profile['id'])
            controls = self.account_controls[profile['slot']]
            if not controls['name'].hasFocus():
                controls['name'].setText(profile.get('name') or '')
            summary = next((p for p in stats.get('profiles', []) if p['slot'] == profile['slot']), {})
            controls['stats'].setText(f"Выполнено {summary.get('completed', 0)} · публикаций {summary.get('published', 0)} · осталось {summary.get('remaining', 0)}")
            try:
                path = self.service.browser.profile_path(profile['slot'])
                controls['path'].setText('Локальный профиль: ' + str(path))
            except (AttributeError, TypeError):
                controls['path'].setText('Отдельный постоянный профиль Chromium')
        self.profile_choice.setCurrentIndex(max(0, self.profile_choice.findData(selected)))
        self.profile_choice.blockSignals(False)
        self._reload_tasks()
        bank = self.service.bank.stats()
        self.bank_summary.setText(f"Всего: {bank.get('total', 0)} · использовано: {bank.get('used', 0)} · доступно: {bank.get('available', 0)}")
        self.bank_list.setPlainText('\n'.join(('✓ ' if comment.get('used') else '○ ') + comment['text'] for comment in self.service.bank.list_comments()))
        self._busy(self.worker is not None)

    def _profile_changed(self):
        if self.profile_choice.currentData():
            self.service.store.set_setting('gui_profile', self.profile_choice.currentData())
        self.current_task = None
        self._reload_tasks()

    def select_profile_slot(self, slot):
        if self.worker is not None:
            return
        for index, profile in enumerate(self.service.profiles()):
            if profile['slot'] == slot:
                self.profile_choice.setCurrentIndex(index)
                self.navigate(0)
                return

    def _reload_tasks(self, preferred=None):
        rows = self._task_rows()
        current_id = preferred or (self.current_task or {}).get('id')
        if current_id is None:
            current_id = self.service.store.get_setting('gui_task_' + str(self.profile_choice.currentData()), None)
        selected = next((row for row in rows if row['id'] == current_id and row.get('status') != 'DONE'), None)
        if selected is None:
            selected = next((row for row in rows if row.get('status') != 'DONE'), rows[-1] if rows else None)
        self.task_choice.blockSignals(True)
        self.task_choice.clear()
        for index, row in enumerate(rows, 1):
            self.task_choice.addItem(f"Видео {index} · {row['video_id']} · {self._status_text(row)}", row['id'])
        if selected:
            self.task_choice.setCurrentIndex(self.task_choice.findData(selected['id']))
        self.task_choice.blockSignals(False)
        self.current_task = selected
        self._render_task()

    def _task_changed(self):
        task_id = self.task_choice.currentData()
        self.current_task = next((row for row in self._task_rows() if row['id'] == task_id), None)
        if task_id is not None:
            self.service.store.set_setting('gui_task_' + str(self.profile_choice.currentData()), task_id)
        self._render_task()

    @staticmethod
    def _status_text(task):
        return {'WAITING': 'Нужен комментарий', 'READY': 'Готово к заполнению', 'FILLED': 'Текст подставлен',
                'PUBLISHED': 'Публикация подтверждена', 'DONE': 'Выполнено', 'FAILED': 'Нужно повторить'}.get(task.get('status'), task.get('status', ''))

    def _render_task(self, enabled=None):
        enabled = self.worker is None and not self._closing if enabled is None else enabled
        task = self.current_task
        self.current_url.setText(task.get('url', '') if task else '')
        self.comment_text.setPlainText(task.get('comment', '') if task else '')
        self.task_title.setText(f"Аккаунт {task['slot']} · текущее задание" if task else 'Текущее задание')
        text = self._status_text(task) if task else 'Заданий пока нет. Вставьте ссылки и нажмите «СТАРТ».'
        if task and task.get('error'):
            text += '\n' + safe_message(task['error'])
        self.task_status.setText(text)
        published = bool(task and task.get('published'))
        liked = bool(task and task.get('liked'))
        filled = bool(task and task.get('status') == 'FILLED')
        self.fill_button.setEnabled(enabled and bool(task and task.get('comment')) and not published)
        self.manual_fill_button.setEnabled(enabled and bool(task and task.get('comment')) and not published and not filled)
        self.copy_button.setEnabled(bool(task and task.get('comment')))
        self.publish_button.setEnabled(enabled and filled and not published)
        self.like_button.setEnabled(enabled and published and not liked)
        self.next_button.setEnabled(enabled and bool(task))

    def start(self):
        if self.worker is not None:
            return
        text = self.links_input.toPlainText().strip()
        if not text and not self.service.tasks(pending_only=True):
            self._error('Вставьте ссылки TikTok — каждую с новой строки.')
            return
        self.service.store.set_setting('gui_links', text)
        self.stop_event.clear()
        try:
            # Resume is local and happens before worker dispatch. A subsequent
            # STOP/PAUSE must win even if the worker has not started yet.
            self.service.resume()
        except Exception as error:
            self._error(error)
            return
        def prepare():
            imported = self.service.import_links(text, stop_event=self.stop_event) if text else {'added': 0, 'duplicates': 0, 'rejected': 0, 'errors': []}
            if self.stop_event.is_set():
                return {'import': imported, 'assignment': {}}
            return {'import': imported, 'assignment': self.service.prepare()}
        self._launch('Сохраняем ссылки и назначаем ваши комментарии…', prepare, self._prepared)

    def _prepared(self, result):
        imported, assignment = result.get('import', {}), result.get('assignment', {})
        message = f"Новых видео: {imported.get('added', 0)} · дубликатов: {imported.get('duplicates', 0)} · отклонено: {imported.get('rejected', 0)}"
        if imported.get('legacy_published'):
            message += f"\nРанее опубликованные видео прежней версии: {imported['legacy_published']} — пропущены."
        missing = assignment.get('missing', 0)
        if missing:
            message += f'\nНе хватает комментариев: {missing}. Добавьте тексты в «Мои комментарии» и продолжите.'
        errors = imported.get('errors') or []
        if errors:
            message += '\n' + '\n'.join(map(str, errors[:4]))
        self.import_summary.setText(message)
        if self.stop_event.is_set() or self._state() != 'RUNNING':
            self.status_label.setText('Обработка остановлена. Всё подготовленное сохранено.')
            return
        if self.current_task and self.current_task.get('comment'):
            if self.current_task.get('published'):
                self.status_label.setText('Публикация уже подтверждена. Поставьте лайк в TikTok и подтвердите его.')
            else:
                self.open_current()
        else:
            self.status_label.setText('Добавьте недостающие комментарии или выберите другую очередь аккаунта.')

    def open_current(self):
        task = self.current_task
        if not task or not task.get('comment') or task.get('published'):
            return
        if self._state() != 'RUNNING':
            self._error('Нажмите «СТАРТ», чтобы продолжить очередь.')
            return
        task_id = task['id']
        self._launch('Playwright открывает ролик и заполняет поле. Публикацию подтвердите вручную.',
                     lambda: self.service.open_task(task_id, stop_event=self.stop_event), self._opened)

    def _opened(self, result):
        if self.stop_event.is_set():
            self.status_label.setText('Обработка приостановлена. Неподтверждённые действия не учтены.')
            return
        self.status_label.setText(result.get('message') or 'Текст подставлен. Проверьте его и опубликуйте самостоятельно в TikTok.')
        self.home_area.ensureWidgetVisible(self.publish_button, 0, 24)

    def pause(self):
        self.stop_event.set()
        self.service.pause()
        self.status_label.setText('ПАУЗА · назначения и подтверждённые действия сохранены. Для продолжения нажмите «СТАРТ».')
        self._render_task()

    def stop(self):
        self.stop_event.set()
        self.service.stop()
        self.status_label.setText('СТОП · очередь сохранена. Для продолжения нажмите «СТАРТ».')
        self._render_task()

    def confirm_publication(self):
        task = self.current_task
        if not task or self.worker is not None or task.get('status') != 'FILLED':
            return
        if QMessageBox.question(self, 'Подтвердите публикацию',
            f"Вы самостоятельно опубликовали этот комментарий в TikTok от аккаунта {task['slot']}?\n\n{task['comment']}\n\nПодтверждайте только после успешной публикации в браузере.") != QMessageBox.StandardButton.Yes:
            return
        try:
            self.service.confirm_publication(task['id'])
            self.reload()
            self.status_label.setText('Публикация записана. Поставьте лайк своему комментарию в TikTok и подтвердите его.')
        except Exception as error:
            self._error(error)

    def confirm_manual_fill(self):
        task = self.current_task
        if not task or self.worker is not None or not task.get('comment') or task.get('published'):
            return
        if QMessageBox.question(self, 'Подтвердите ручную вставку',
            f"Вы открыли нужный ролик от аккаунта {task['slot']} и вручную вставили этот текст в поле комментария?\n\n{task['comment']}\n\n{task['url']}\n\nЭто подтверждает только вставку. Публикацию нужно выполнить отдельно в TikTok.") != QMessageBox.StandardButton.Yes:
            return
        try:
            self.service.confirm_manual_fill(task['id'])
            self.reload()
            self.status_label.setText('Ручная вставка записана. Опубликуйте комментарий в TikTok и затем подтвердите публикацию.')
        except Exception as error:
            self._error(error)

    def confirm_like(self):
        task = self.current_task
        if not task or self.worker is not None or not task.get('published') or task.get('liked'):
            return
        if QMessageBox.question(self, 'Подтвердите лайк',
            f"Вы самостоятельно поставили лайк своему опубликованному комментарию от аккаунта {task['slot']} в TikTok?") != QMessageBox.StandardButton.Yes:
            return
        try:
            self.service.confirm_like(task['id'])
            self.reload()
        except Exception as error:
            self._error(error)
            return
        self.drain_telegram(advance=True)

    def next_task(self):
        if self.worker is not None:
            return
        rows = [row for row in self._task_rows() if row.get('status') != 'DONE']
        if not rows:
            self.status_label.setText('Очередь этого аккаунта завершена. Выберите второй аккаунт, если у него остались задания.')
            return
        current_id = (self.current_task or {}).get('id')
        index = next((index for index, row in enumerate(rows) if row['id'] == current_id), -1)
        selected = rows[(index + 1) % len(rows)]
        self.task_choice.setCurrentIndex(self.task_choice.findData(selected['id']))
        if self._state() == 'RUNNING' and not selected.get('published'):
            self.open_current()

    def import_comments(self):
        text = self.bank_input.toPlainText()
        def add():
            result = self.service.bank.import_text(text)
            result['assignment'] = self.service.prepare()
            return result
        self._launch('Сохраняем ваш банк комментариев…', add, self._comments_added)

    def _comments_added(self, result):
        message = f"Добавлено: {result.get('added', 0)} · дубликатов: {result.get('duplicates', 0)} · отклонено: {result.get('rejected', 0)}"
        if result.get('errors'):
            message += '\n' + '\n'.join(map(str, result['errors'][:8]))
            message += '\nИсправьте отклонённые строки и повторите: уже добавленные тексты не дублируются.'
        self.bank_feedback.setText(message)
        if not result.get('rejected'):
            self.bank_input.clear()
        self.status_label.setText('Банк сохранён. Нажмите «СТАРТ», чтобы продолжить задания.')

    def reset_usage(self):
        if self.worker is not None:
            return
        if QMessageBox.question(self, 'Сбросить историю использования?',
            'Завершённые комментарии смогут снова использоваться для будущих видео. Текущие назначения сохранятся. Один и тот же комментарий не назначается двум аккаунтам одного видео. Продолжить?') != QMessageBox.StandardButton.Yes:
            return
        self._launch('Сбрасываем историю использования…', self.service.bank.reset_usage,
                     lambda result: self.status_label.setText('История сброшена. Текущие задания сохранены.'))

    def save_profile(self, slot):
        try:
            self.service.store.save_profile(slot, self.account_controls[slot]['name'].text())
            self.reload()
            self.status_label.setText('Название аккаунта сохранено. Вход выполняется отдельно в браузере TikTok.')
        except Exception as error:
            self._error(error)

    def open_login(self, slot):
        if self.test_mode:
            self._error('В тестовом режиме реальный вход в TikTok отключён.')
            return
        self._launch(f'Открываем официальный TikTok для аккаунта {slot}…',
                     lambda: self.service.open_login(slot),
                     lambda result: self.status_label.setText(result.get('message') or 'Войдите самостоятельно в открывшемся окне TikTok.'))

    def save_telegram(self):
        token, chat, enabled = self.telegram_token.text(), self.telegram_chat.text(), self.telegram_enabled.isChecked()
        def save():
            self.service.save_telegram(token, chat, enabled)
            return self.service.drain_telegram()
        self._launch('Сохраняем настройки Telegram…', save, self._delivery_result)

    def drain_telegram(self, advance=False):
        def delivered(result):
            self._delivery_result(result)
            if advance and not self.stop_event.is_set() and self._state() == 'RUNNING':
                # Reload already selected the next unfinished task. It is opened
                # only after the previous task's explicit like confirmation.
                if self.current_task and self.current_task.get('status') not in ('DONE', 'PUBLISHED'):
                    QTimer.singleShot(0, self.open_current)
        self._launch('Проверяем подтверждённые ссылки для Telegram…', self.service.drain_telegram, delivered)

    def _delivery_result(self, result):
        errors = result.get('errors') or []
        text = f"Telegram: отправлено сейчас {result.get('sent', 0)}. Очередь сохранена."
        if errors:
            text += '\n' + '\n'.join(map(safe_message, errors[:3]))
        self.status_label.setText(text)

    def _startup_delivery(self):
        if self.worker is None and not self._closing and not self.test_mode:
            if self.service.telegram_settings().get('enabled'):
                self.drain_telegram()

    def open_demo(self):
        if self.test_mode or self.worker is not None:
            return
        def create_demo():
            from automation_service import AutomationService
            demo_service = AutomationService(self.service.config_path, test_mode=True,
                                             root=getattr(self.service, 'source_root', self.service.root))
            try:
                demo_service.demo()
                return demo_service
            except Exception:
                demo_service.close()
                raise
        def show_demo(demo_service):
            window = AutomationWindow(demo_service)
            window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
            self._demo_windows.append(window)
            window.destroyed.connect(lambda: self._demo_windows.remove(window) if window in self._demo_windows else None)
            window.show()
            self.status_label.setText('Открыта отдельная демонстрация на 50 видео. Рабочая очередь сохранена.')
        self._launch('Готовим отдельный тестовый режим без браузера и Telegram…', create_demo, show_demo)

    def _close_in_background(self):
        def close_resources():
            try:
                if self._extra_close_service is not None:
                    self._extra_close_service.close()
            finally:
                self.service.close()
        closer = AutomationWorker(close_resources, self)
        self.worker = closer
        closer.finished.connect(self._closed_resources)
        closer.start()

    def _closed_resources(self):
        worker, self.worker = self.worker, None
        if worker:
            worker.deleteLater()
        self._allow_close = True
        self.close()

    def closeEvent(self, event):
        if self._allow_close:
            event.accept()
            return
        event.ignore()
        if self._closing:
            return
        self._closing = True
        self.stop_event.set()
        try:
            self.service.stop()
        except Exception:
            pass
        for window in list(self._demo_windows):
            window.close()
        self.status_label.setText('Закрываем браузерные окна и сохраняем очередь…')
        self._busy(True)
        if self.worker is None:
            self._close_in_background()
