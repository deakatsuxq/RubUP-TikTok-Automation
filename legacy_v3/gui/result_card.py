"""A readable prepared result; unknown checks are visible beside the draft."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout


class ResultCard(QFrame):
    copy_requested = Signal()
    regenerate_requested = Signal()
    open_requested = Signal()
    published_requested = Signal()
    review_requested = Signal()
    details_requested = Signal()
    image_requested = Signal()
    send_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('card')
        box = QVBoxLayout(self)
        box.setContentsMargins(22, 20, 22, 20)
        box.setSpacing(12)
        head = QHBoxLayout()
        self.title = QLabel('Видео готово к проверке')
        self.title.setObjectName('sectionTitle')
        head.addWidget(self.title)
        head.addStretch()
        self.badge = QLabel('Комментарий готов')
        self.badge.setObjectName('statusBadge')
        head.addWidget(self.badge)
        box.addLayout(head)
        self.url = QLabel()
        self.url.setObjectName('muted')
        self.url.setTextFormat(Qt.TextFormat.PlainText)
        self.url.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.url.setWordWrap(True)
        box.addWidget(self.url)
        self.comment_editor = QPlainTextEdit()
        self.comment_editor.setObjectName('preparedComment')
        self.comment_editor.setPlaceholderText('Здесь появится подготовленный комментарий')
        self.comment_editor.setMinimumHeight(96)
        self.comment_editor.setMaximumHeight(140)
        box.addWidget(self.comment_editor)
        self.checks = QLabel()
        self.checks.setWordWrap(True)
        self.checks.setObjectName('muted')
        box.addWidget(self.checks)
        self.warning = QLabel()
        self.warning.setObjectName('warning')
        self.warning.setWordWrap(True)
        self.warning.hide()
        box.addWidget(self.warning)
        self.image_label = QLabel()
        self.image_label.setObjectName('muted')
        box.addWidget(self.image_label)
        actions = QGridLayout()
        self.buttons = {}
        for i, (key, title, signal) in enumerate((
            ('copy', 'Скопировать комментарий', self.copy_requested),
            ('regenerate', 'Сгенерировать другой', self.regenerate_requested),
            ('open', 'Открыть TikTok', self.open_requested),
            ('published', 'Я опубликовал', self.published_requested),
        )):
            button = QPushButton(title)
            if key == 'copy':
                button.setObjectName('primary')
            button.clicked.connect(signal.emit)
            actions.addWidget(button, 0, i)
            self.buttons[key] = button
        box.addLayout(actions)
        secondary = QHBoxLayout()
        self.review_button = QPushButton('Проверить недостающие сведения')
        self.review_button.setObjectName('linkButton')
        self.review_button.clicked.connect(self.review_requested.emit)
        secondary.addWidget(self.review_button)
        self.image_button = QPushButton('Показать нужную картинку')
        self.image_button.setObjectName('linkButton')
        self.image_button.clicked.connect(self.image_requested.emit)
        secondary.addWidget(self.image_button)
        secondary.addStretch()
        self.send_button = QPushButton('Отправить ссылку в Telegram')
        self.send_button.clicked.connect(self.send_requested.emit)
        secondary.addWidget(self.send_button)
        self.details_button = QPushButton('Подробности задания')
        self.details_button.setObjectName('linkButton')
        self.details_button.clicked.connect(self.details_requested.emit)
        secondary.addWidget(self.details_button)
        box.addLayout(secondary)
        self.task = None

    def set_task(self, task, status):
        self.task = dict(task)
        demo = bool(task.get('demo') or task.get('is_test'))
        self.title.setText('Пример обработки' if demo else ('Видео готово к проверке' if task.get('comment') else 'Задание ожидает подготовки'))
        self.badge.setText('Тестовый пример' if demo else status)
        self.url.setText(task.get('url', ''))
        self.comment_editor.setPlainText(task.get('comment') or '')
        review = task.get('review') or {}
        missing = review.get('missing') or []
        eligible = review.get('eligible', False)
        date = task.get('published_at')
        lines = [f"Дата: {date}" if date else 'Дата пока неизвестна']
        lines.append('Тестовые сведения — проверка реального видео не выполнялась' if demo
                     else 'Требования проверены' if eligible
                     else 'Предварительный черновик — нужна проверка' if review.get('preliminary_allowed')
                     else 'Требования пока не выполнены')
        self.checks.setText(' · '.join(lines))
        message = task.get('warning') or (review.get('reason') if not eligible else '') or ''
        if not task.get('comment'):
            message = task.get('error') or message or 'Комментарий ещё не подготовлен. Продолжите обработку задания.'
        self.warning.setText(message)
        self.warning.setVisible(bool(message))
        slot = task.get('image_slot')
        self.image_label.setText(f'Нужно прикрепить вручную: картинка №{slot}' if slot else 'Картинка не требуется')
        self.image_button.setVisible(bool(slot) and not demo)
        self.review_button.setVisible((bool(missing) or not eligible) and not demo)
        published = task.get('publication_status') == 'PUBLISHED'
        self.send_button.setVisible(published and not demo)
        self.buttons['published'].setText('Публикация подтверждена' if published else 'Я опубликовал')
        self.buttons['published'].setEnabled(not demo and not published and bool(task.get('comment')))
        self.buttons['open'].setEnabled(not demo)
        self.buttons['regenerate'].setEnabled(not published)
        self.buttons['copy'].setEnabled(bool(task.get('comment')))
        self.details_button.setVisible(not demo)
        self.comment_editor.setReadOnly(published or demo)

    def set_busy(self, busy):
        for button in list(self.buttons.values()) + [self.review_button, self.image_button,
                                                     self.send_button, self.details_button]:
            button.setEnabled(not busy)
        self.comment_editor.setReadOnly(busy)
        if not busy and self.task:
            # Restore guards without resetting a user's in-progress edit.
            demo = bool(self.task.get('demo') or self.task.get('is_test'))
            published = self.task.get('publication_status') == 'PUBLISHED'
            self.buttons['published'].setEnabled(not demo and not published and bool(self.task.get('comment')))
            self.buttons['open'].setEnabled(not demo)
            self.buttons['regenerate'].setEnabled(not published)
            self.buttons['copy'].setEnabled(bool(self.task.get('comment')))
            self.comment_editor.setReadOnly(demo or published)
