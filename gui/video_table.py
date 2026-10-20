"""Sortable and filterable tasks. Selection uses task IDs, never row numbers."""
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QSortFilterProxyModel, Signal
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableView

LABELS = {
    "NEW": "Найдено", "ANALYZED": "Проверяется", "DRAFT_READY": "Комментарий готов",
    "PENDING_APPROVAL": "Ожидает публикации", "PUBLISHED": "Опубликовано",
    "FAILED": "Ошибка", "SKIPPED": "Пропущено", "UNCONFIRMED": "Не подтверждено",
    "NOT_SENT": "Не отправлено", "SENT": "Ссылка отправлена", "UNKNOWN": "Неизвестно",
    "NONE": "Нет", "REPORT_SUBMITTED": "Отчёт подан", "REPORT_ACCEPTED": "Отчёт принят",
    "REWARD_CONFIRMED": "Задание завершено",
}


class TaskModel(QAbstractTableModel):
    COLUMNS = (
        ("video_id", "ID видео"), ("author", "Автор"), ("published_at", "Дата публикации"),
        ("hashtags", "Хештеги"), ("url", "URL"), ("comment", "Комментарий"),
        ("image_slot", "Картинка"), ("processing_status", "Обработка"),
        ("publication_status", "Публикация"), ("telegram_status", "Telegram"), ("error", "Ошибка"),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tasks = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.tasks)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.COLUMNS)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.tasks):
            return None
        task = self.tasks[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return task
        key = self.COLUMNS[index.column()][0]
        value = task.get(key)
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            if value is None:
                return "—"
            if isinstance(value, list):
                return " ".join(str(x) for x in value)
            if key.endswith("status"):
                return LABELS.get(str(value), str(value))
            return str(value)
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.COLUMNS[section][1]
        return super().headerData(section, orientation, role)

    def set_tasks(self, tasks):
        self.beginResetModel()
        self.tasks = list(tasks)
        self.endResetModel()


class TaskFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.query = ""
        self.status = ""
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def set_query(self, query):
        self.query = query.casefold().strip()
        self.invalidate()

    def set_status(self, status):
        self.status = status
        self.invalidate()

    def filterAcceptsRow(self, row, parent):
        task = self.sourceModel().tasks[row]
        if self.status and task.get("processing_status") != self.status:
            return False
        return not self.query or self.query in " ".join(str(v) for v in task.values()).casefold()


class VideoTable(QTableView):
    task_selected = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.task_model = TaskModel(self)
        self.proxy = TaskFilter(self)
        self.proxy.setSourceModel(self.task_model)
        self.setModel(self.proxy)
        self.setSortingEnabled(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.setWordWrap(False)
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.horizontalHeader().setStretchLastSection(True)
        self.setColumnWidth(0, 150)
        self.setColumnWidth(2, 140)
        self.setColumnWidth(3, 190)
        self.setColumnWidth(4, 240)
        self.setColumnWidth(5, 300)
        self.setMinimumHeight(210)
        self.selectionModel().selectionChanged.connect(self._selection)

    def _selection(self, *_):
        rows = self.selectionModel().selectedRows()
        if rows:
            source = self.proxy.mapToSource(rows[0])
            self.task_selected.emit(dict(self.task_model.tasks[source.row()]))

    def set_tasks(self, tasks, selected_id=None):
        self.task_model.set_tasks(tasks)
        if selected_id is not None:
            for row, task in enumerate(self.task_model.tasks):
                if task.get("id") == selected_id:
                    index = self.proxy.mapFromSource(self.task_model.index(row, 0))
                    if index.isValid():
                        self.selectRow(index.row())
                    break
