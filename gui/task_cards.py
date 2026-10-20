"""Readable task history; the database retains its original independent states."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)


def status_label(task: dict) -> str:
    """Describe an actually reached stage without inferring publication/payment."""
    if task.get("reward_status") == "REWARD_CONFIRMED" and task.get("report_status") == "REPORT_ACCEPTED":
        return "Задание завершено"
    if task.get("processing_status") == "FAILED":
        return "Ошибка"
    if task.get("report_status") == "REPORT_ACCEPTED":
        return "Отчёт принят"
    if task.get("report_status") == "REPORT_SUBMITTED":
        return "Отчёт отправлен"
    if task.get("telegram_status") == "SENT":
        return "Ссылка отправлена"
    if task.get("publication_status") == "PUBLISHED":
        return "Опубликовано"
    state = task.get("processing_status", "NEW")
    return {
        "NEW": "Найдено", "ANALYZED": "Проверено",
        "DRAFT_READY": "Комментарий готов", "PENDING_APPROVAL": "Ожидает публикации",
        "SKIPPED": "Пропущено", "PUBLISHED": "Опубликовано",
    }.get(state, "Найдено")


def _date_label(value) -> str:
    if not value:
        return "Дата добавления неизвестна"
    try:
        date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return date.strftime("%d.%m.%Y · %H:%M")
    except (TypeError, ValueError):
        return "Дата добавления неизвестна"


class TaskCard(QFrame):
    open_task = Signal(dict)
    resume_task = Signal(dict)

    def __init__(self, task, parent=None):
        super().__init__(parent)
        self.task = dict(task)
        self.setObjectName("taskCard")
        box = QVBoxLayout(self)
        box.setContentsMargins(22, 18, 22, 18)
        box.setSpacing(10)
        header = QHBoxLayout()
        title = QLabel("Видео TikTok")
        title.setObjectName("cardTitle")
        header.addWidget(title)
        header.addStretch()
        self.status = QLabel(status_label(task))
        self.status.setObjectName("statusBadge")
        header.addWidget(self.status)
        box.addLayout(header)
        details = QLabel(_date_label(task.get("created_at") or task.get("discovered_at")))
        details.setObjectName("muted")
        box.addWidget(details)
        comment = str(task.get("comment") or "Комментарий ещё не подготовлен")
        self.comment = QLabel(comment[:250] + ("…" if len(comment) > 250 else ""))
        self.comment.setWordWrap(True)
        self.comment.setTextFormat(Qt.TextFormat.PlainText)
        self.comment.setObjectName("taskComment")
        box.addWidget(self.comment)
        meta = QHBoxLayout()
        slot = task.get("image_slot")
        image = QLabel(f"Картинка №{slot}" if slot else "Без картинки")
        image.setObjectName("muted")
        meta.addWidget(image)
        if (task.get("checklist") or {}).get("preliminary"):
            pending = QLabel("Нужна проверка данных")
            pending.setObjectName("warningText")
            meta.addWidget(pending)
        meta.addStretch()
        self.open_button = QPushButton("Открыть")
        self.open_button.clicked.connect(lambda: self.open_task.emit(dict(self.task)))
        meta.addWidget(self.open_button)
        self.resume_button = QPushButton("Продолжить")
        self.resume_button.setObjectName("secondary")
        self.resume_button.setEnabled(task.get("publication_status") != "PUBLISHED")
        self.resume_button.clicked.connect(lambda: self.resume_task.emit(dict(self.task)))
        meta.addWidget(self.resume_button)
        box.addLayout(meta)


class TaskCards(QWidget):
    open_task = Signal(dict)
    resume_task = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks = []
        self._query = ""
        self._filter = "all"
        self._limit = 40
        self.cards = []
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)
        self.count_label = QLabel()
        self.count_label.setObjectName("muted")
        root.addWidget(self.count_label)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content.setObjectName("pageContent")
        self.container = QVBoxLayout(content)
        self.container.setContentsMargins(0, 0, 8, 0)
        self.container.setSpacing(12)
        self.container.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(content)
        root.addWidget(self.scroll, 1)
        self._rebuild()

    def set_tasks(self, tasks):
        self._tasks = [dict(task) for task in tasks]
        self._rebuild()

    def set_query(self, query):
        self._query = str(query).strip().casefold()
        self._limit = 40
        self._rebuild()

    def set_filter(self, value):
        self._filter = value if value in {"all", "ready", "wait", "published", "errors"} else "all"
        self._limit = 40
        self._rebuild()

    def _matches(self, task):
        if self._query:
            values = (task.get(key) for key in ("video_id", "url", "author", "description", "comment"))
            if self._query not in " ".join(str(value or "") for value in values).casefold():
                return False
        if self._filter == "ready":
            return bool(task.get("comment"))
        if self._filter == "wait":
            return bool(task.get("comment")) and task.get("publication_status") != "PUBLISHED"
        if self._filter == "published":
            return task.get("publication_status") == "PUBLISHED"
        if self._filter == "errors":
            return task.get("processing_status") == "FAILED"
        return True

    def _rebuild(self):
        while self.container.count():
            item = self.container.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.cards = []
        tasks = [task for task in reversed(self._tasks) if self._matches(task)]
        self.count_label.setText(f"Показано заданий: {min(len(tasks), self._limit)} из {len(tasks)}")
        if not tasks:
            empty = QFrame()
            empty.setObjectName("emptyState")
            layout = QVBoxLayout(empty)
            layout.setContentsMargins(30, 36, 30, 36)
            title = QLabel("Заданий пока нет" if not self._tasks else "Ничего не найдено")
            title.setObjectName("cardTitle")
            title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            explanation = QLabel("Добавьте ссылку на главной странице" if not self._tasks else "Измените фильтр или поисковый запрос")
            explanation.setObjectName("muted")
            explanation.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(title)
            layout.addWidget(explanation)
            self.container.addWidget(empty)
            return
        for task in tasks[:self._limit]:
            card = TaskCard(task)
            card.open_task.connect(self.open_task)
            card.resume_task.connect(self.resume_task)
            self.container.addWidget(card)
            self.cards.append(card)
        if len(tasks) > self._limit:
            more = QPushButton("Показать ещё")
            more.clicked.connect(self._show_more)
            self.container.addWidget(more)

    def _show_more(self):
        self._limit += 40
        self._rebuild()
