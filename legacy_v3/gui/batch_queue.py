"""Video cards containing two independently tracked manual actions."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMenu, QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget


def task_complete(task):
    checks = task.get('checklist') or {}
    return task.get('publication_status') == 'PUBLISHED' and checks.get('comment_present') is True and checks.get('comment_not_deleted') is True and (not checks.get('require_like') or checks.get('own_comment_liked') is True) and (not checks.get('require_image') or checks.get('image_attached') is True)


def task_status(task):
    if task_complete(task): return 'Выполнено'
    if task.get('publication_status') == 'PUBLISHED': return 'Опубликовано — остались требования'
    if task.get('processing_status') in ('FAILED', 'SKIPPED'): return 'Нужно проверить ошибку'
    if task.get('comment'): return 'Комментарий готов' if (task.get('review') or {}).get('eligible') else 'Черновик — нужна проверка'
    return 'Ожидает подготовки'


class VideoCard(QFrame):
    action = Signal(str, int)
    review = Signal(object)
    next_requested = Signal()
    activated = Signal(object)

    def __init__(self, group, images=None, demo=False, parent=None):
        super().__init__(parent)
        self.group, self.demo, self.panels = group, demo or bool(group.get('is_test')), {}
        self.setObjectName('card'); root = QVBoxLayout(self); root.setContentsMargins(20, 18, 20, 18); root.setSpacing(12)
        title = QHBoxLayout(); heading = QLabel('Тестовое видео' if self.demo else 'Видео TikTok'); heading.setObjectName('sectionTitle'); title.addWidget(heading); title.addStretch()
        self.next_button = QPushButton('Следующее видео →'); self.next_button.setToolTip('Следующее незавершённое видео: Ctrl+→'); self.next_button.clicked.connect(self.next_requested.emit); title.addWidget(self.next_button); root.addLayout(title)
        url = QLabel(group.get('url') or group.get('source_url') or ''); url.setTextFormat(Qt.TextFormat.PlainText); url.setWordWrap(True); url.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse); url.setObjectName('muted'); root.addWidget(url)
        review = group.get('review') or {}; row = QHBoxLayout()
        text = 'Тестовые сведения — не проверка настоящего видео' if self.demo else 'Дата и требования проверены' if review.get('eligible') else review.get('reason') or 'Сведения ещё не проверены'
        label = QLabel(text); label.setWordWrap(True); label.setObjectName('muted'); row.addWidget(label, 1)
        self.review_button = QPushButton('Проверить сведения'); self.review_button.clicked.connect(lambda: self.review.emit(self.group)); self.review_button.setEnabled(not self.demo); row.addWidget(self.review_button); root.addLayout(row)
        if group.get('error'):
            error = QLabel(group['error']); error.setWordWrap(True); error.setTextFormat(Qt.TextFormat.PlainText); error.setObjectName('warning'); root.addWidget(error)
        grid = QGridLayout(); grid.setHorizontalSpacing(16)
        tasks = sorted(group.get('tasks') or [], key=lambda t: t.get('slot', (t.get('profile') or {}).get('slot', 0)))
        for index, task in enumerate(tasks):
            slot = task.get('slot', (task.get('profile') or {}).get('slot', index + 1)); profile = task.get('profile') or {}
            panel = QFrame(); panel.setObjectName('imageCard'); body = QVBoxLayout(panel); body.setContentsMargins(15, 15, 15, 15)
            title = QLabel(f'Аккаунт №{slot}' + (f" · @{profile['username']}" if profile.get('username') else '')); title.setObjectName('sectionTitle'); body.addWidget(title)
            state = QLabel('Тестовый черновик' if self.demo else task_status(task)); state.setWordWrap(True); state.setObjectName('muted'); body.addWidget(state)
            comment = QPlainTextEdit(task.get('comment') or 'Комментарий ещё не подготовлен'); comment.setReadOnly(True); comment.setMinimumHeight(85); comment.setMaximumHeight(110); body.addWidget(comment)
            if task.get('error'):
                error = QLabel(task['error']); error.setWordWrap(True); error.setTextFormat(Qt.TextFormat.PlainText); error.setObjectName('warning'); body.addWidget(error)
            if task.get('image_slot'):
                image_row = QHBoxLayout(); image_label = QLabel(f"Картинка №{task['image_slot']} — прикрепите вручную"); image_label.setWordWrap(True); image_row.addWidget(image_label)
                if images is not None and not self.demo:
                    image = images.get_image(task['image_slot'])
                    if image:
                        preview = QLabel(); preview.setPixmap(QPixmap(image['path']).scaled(75, 50, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)); image_row.addWidget(preview)
                opening = QPushButton('Открыть файл'); opening.setEnabled(not self.demo); opening.clicked.connect(lambda checked=False, t=task: self._action('image', t)); image_row.addWidget(opening); body.addLayout(image_row)
            controls = QGridLayout(); buttons = {}
            for row_num, col, key, title in [(0, 0, 'open', 'Открыть TikTok'), (0, 1, 'copy', 'Скопировать комментарий'), (1, 0, 'publish', 'Я опубликовал'), (1, 1, 'like', 'Я поставил лайк')]:
                button = QPushButton(title); button.clicked.connect(lambda checked=False, a=key, t=task: self._action(a, t))
                if key == 'copy': button.setObjectName('primary'); button.setEnabled(bool(task.get('comment'))); button.setToolTip(f'Копирование: Ctrl+{slot}')
                elif key == 'publish': button.setEnabled(not self.demo and bool(task.get('comment')) and task.get('publication_status') != 'PUBLISHED')
                elif key == 'like': button.setEnabled(not self.demo and task.get('publication_status') == 'PUBLISHED' and not (task.get('checklist') or {}).get('own_comment_liked')); button.setToolTip('Подтверждайте только действительно поставленный вами лайк.')
                else: button.setEnabled(not self.demo)
                controls.addWidget(button, row_num, col); buttons[key] = button
            body.addLayout(controls)
            extra = QPushButton('Другие действия…'); menu = QMenu(extra)
            for key, caption in [('retry', 'Повторить неуспешную подготовку'), ('edit', 'Изменить комментарий'), ('regenerate', 'Сгенерировать другой вариант'), ('requirements', 'Требования картинки и лайка')]:
                action = menu.addAction(caption); action.setEnabled(not self.demo); action.triggered.connect(lambda checked=False, a=key, t=task: self._action(a, t))
            extra.setMenu(menu); body.addWidget(extra); grid.addWidget(panel, 0, index)
            self.panels[slot] = dict(task=task, buttons=buttons, comment=comment, status=state, extra=extra)
        if not tasks:
            note = QLabel('Ссылка сохранена. Подготовка определит видео и создаст задания для двух аккаунтов.'); note.setWordWrap(True); root.addWidget(note)
        root.addLayout(grid)

    def _action(self, name, task):
        self.activated.emit(self.group); self.action.emit(name, task['id'])


class BatchQueue(QWidget):
    action_requested = Signal(str, int)
    review_requested = Signal(object)

    def __init__(self, images=None, parent=None):
        super().__init__(parent)
        self.images, self.groups, self.cards, self.current_index, self.demo = images, [], [], 0, False
        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0)
        title = QLabel('Очередь публикаций'); title.setObjectName('pageTitle'); root.addWidget(title)
        hint = QLabel('Один ролик — два независимых комментария. Выполните публикации вручную, подтвердите лайки, если они требуются.'); hint.setWordWrap(True); hint.setObjectName('muted'); root.addWidget(hint)
        row = QHBoxLayout(); self.search = QLineEdit(); self.search.setPlaceholderText('Найти видео или комментарий'); self.search.textChanged.connect(self._filter); row.addWidget(self.search, 1)
        self.filter = QComboBox()
        for text, value in [('Все', 'all'), ('Готовы к публикации', 'ready'), ('Есть незавершённые', 'pending'), ('Оба опубликованы', 'published'), ('Ошибки', 'errors')]: self.filter.addItem(text, value)
        self.filter.currentIndexChanged.connect(self._filter); row.addWidget(self.filter); root.addLayout(row)
        self.area = QScrollArea(); self.area.setWidgetResizable(True); self.area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.container = QWidget(); self.container.setObjectName('pageContent'); self.card_layout = QVBoxLayout(self.container); self.card_layout.setContentsMargins(0, 8, 8, 8); self.card_layout.setSpacing(18); self.area.setWidget(self.container); root.addWidget(self.area, 1)
        self.empty = QLabel('Вставьте ссылки на главной странице. Очередь сохранится после закрытия программы.'); self.empty.setWordWrap(True); self.empty.setObjectName('muted'); self.card_layout.addWidget(self.empty)

    def set_groups(self, groups, demo=False):
        self.groups, self.demo = list(groups), demo
        for card in self.cards: self.card_layout.removeWidget(card); card.deleteLater()
        self.cards = []
        for group in self.groups:
            card = VideoCard(group, self.images, demo); card.action.connect(self.action_requested); card.review.connect(self.review_requested); card.activated.connect(self._activate); card.next_requested.connect(self.next_unfinished); self.cards.append(card); self.card_layout.addWidget(card)
        self.empty.setVisible(not self.cards); self.current_index = min(self.current_index, max(0, len(self.cards) - 1)); self._filter()

    def _activate(self, group):
        for i, item in enumerate(self.groups):
            if item.get('item_id') == group.get('item_id'): self.current_index = i; break

    def _filter(self):
        needle, mode = self.search.text().strip().casefold(), self.filter.currentData()
        for group, card in zip(self.groups, self.cards):
            tasks = group.get('tasks') or []; text = ' '.join([group.get('url') or group.get('source_url') or ''] + [t.get('comment', '') for t in tasks]).casefold(); matches = not needle or needle in text
            if mode == 'ready': matches = matches and any(t.get('comment') and t.get('publication_status') != 'PUBLISHED' for t in tasks)
            elif mode == 'pending': matches = matches and not (len(tasks) == 2 and all(task_complete(t) for t in tasks))
            elif mode == 'published': matches = matches and len(tasks) == 2 and all(t.get('publication_status') == 'PUBLISHED' for t in tasks)
            elif mode == 'errors': matches = matches and (bool(group.get('error')) or any(t.get('processing_status') in ('FAILED', 'SKIPPED') for t in tasks))
            card.setVisible(bool(matches))

    def next_unfinished(self):
        if not self.cards: return
        for step in range(1, len(self.cards) + 1):
            i = (self.current_index + step) % len(self.cards); tasks = self.groups[i].get('tasks') or []
            if len(tasks) != 2 or not all(task_complete(t) for t in tasks):
                if self.cards[i].isHidden(): self.filter.setCurrentIndex(0); self.search.clear(); self._filter()
                self.current_index = i; self.area.ensureWidgetVisible(self.cards[i], 0, 10); return

    def current_task(self, slot):
        if not self.groups: return None
        return next((t for t in self.groups[self.current_index].get('tasks') or [] if t.get('slot', (t.get('profile') or {}).get('slot')) == slot), None)

    def set_busy(self, busy):
        self.container.setEnabled(not busy)
