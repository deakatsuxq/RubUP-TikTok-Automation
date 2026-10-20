"""Editable non-secret configuration, validated before persistence."""
from dataclasses import replace
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QScrollArea,
    QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)


class SettingsWindow(QDialog):
    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.fields = {}
        self.setWindowTitle("Настройки RubUP")
        self.resize(700, 720)
        root = QVBoxLayout(self)
        notice = QLabel("Ключи API храните в .env. Секреты в этом окне не отображаются и не сохраняются.")
        notice.setWordWrap(True)
        notice.setObjectName("muted")
        root.addWidget(notice)
        tabs = QTabWidget()
        root.addWidget(tabs)
        search = self._page(tabs, "Поиск")
        self._text(search, "hashtags", "Включённые хештеги, по одному в строке", multiline=True)
        self._text(search, "disabled_hashtags", "Отключённые хештеги", multiline=True)
        self._text(search, "keywords", "Ключевые слова, по одному в строке", multiline=True)
        self._text(search, "min_date", "Видео не ранее (ГГГГ-ММ-ДД)")
        self._integer(search, "max_videos", "Максимум задач за цикл", 1, 10000)
        self._integer(search, "search_interval", "Интервал поиска, секунд", 1, 86400)
        self._integer(search, "target_per_minute", "Целевая скорость подготовки / мин", 1, 60)
        process = self._page(tabs, "Обработка")
        self._text(process, "account_id", "ID вашего TikTok-аккаунта (локальный учёт)")
        self._combo(process, "publication_mode", "Публикация", [("Ручное действие", "manual")])
        self._combo(process, "telegram_mode", "Когда отправлять ссылки", [
            ("Отключено", "disabled"), ("После проверки видео", "verified"),
            ("После подготовки комментария", "draft"), ("После подтверждения публикации", "published")])
        self._combo(process, "image_mode", "Выбор изображения", [
            ("Без изображения", "none"), ("По очереди", "rotate"),
            ("Конкретное изображение", "specific"), ("Согласно заданию", "task")])
        self._text(process, "image_order", "Порядок картинок (например 1,2,3,4)")
        self._integer(process, "selected_image", "Конкретная картинка", 1, 4)
        self._check(process, "require_image", "Картинка обязательна для задания")
        self._check(process, "require_like", "Требуется лайк своему комментарию")
        self._check(process, "advertising", "Рекламный комментарий: требовать обозначение")
        self._text(process, "disclosure", "Обозначение рекламы")
        self._check(process, "dry_run", "Изолированный тестовый режим (без реальных отправок)")
        ai = self._page(tabs, "OpenAI и расходы")
        self._text(ai, "model", "Модель")
        self._integer(ai, "max_comment_length", "Максимум символов", 10, 1000)
        self._integer(ai, "max_context_chars", "Максимум символов контекста", 512, 100000)
        self._integer(ai, "max_output_tokens", "Максимум выходных токенов", 16, 4000)
        self._text(ai, "categories", "Категории, по одной в строке", multiline=True)
        self._text(ai, "input_price_per_million", "Цена входа за 1 млн токенов, $ (пусто: неизвестна)")
        self._text(ai, "output_price_per_million", "Цена выхода за 1 млн токенов, $ (пусто: неизвестна)")
        self._decimal(ai, "third_party_cost", "Отдельные расходы источника TikTok, $")
        self._decimal(ai, "reward_default", "Ожидаемое вознаграждение за задачу")
        self._text(ai, "reward_by_image", "Вознаграждение по картинкам (1:0.5,2:1)")
        system = self._page(tabs, "Система")
        self._text(system, "db_path", "Путь SQLite")
        self._combo(system, "log_level", "Уровень локальных логов", [(x, x) for x in ("DEBUG", "INFO", "WARNING", "ERROR")])
        self._integer(system, "retries", "Максимум повторов временных ошибок API", 0, 5)
        self._decimal(system, "timeout", "Тайм-аут API, секунд", 1, 120)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _page(self, tabs, title):
        area = QScrollArea()
        area.setWidgetResizable(True)
        widget = QWidget()
        widget.setObjectName("detailPane")
        form = QFormLayout(widget)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setSpacing(12)
        area.setWidget(widget)
        tabs.addTab(area, title)
        return form

    def _text(self, form, name, title, multiline=False):
        value = getattr(self.service.config, name, "")
        if isinstance(value, list):
            value = ("\n" if multiline else ",").join(str(x) for x in value)
        if isinstance(value, dict):
            value = ",".join(f"{k}:{v}" for k, v in value.items())
        value = "" if value is None else str(value)
        edit = QPlainTextEdit(value) if multiline else QLineEdit(value)
        edit.setObjectName(name)
        if multiline:
            edit.setMaximumHeight(120)
        form.addRow(title, edit)
        self.fields[name] = edit

    def _integer(self, form, name, title, low, high):
        edit = QSpinBox()
        edit.setRange(low, high)
        edit.setValue(getattr(self.service.config, name))
        edit.setObjectName(name)
        form.addRow(title, edit)
        self.fields[name] = edit

    def _decimal(self, form, name, title, low=0, high=1000000):
        edit = QDoubleSpinBox()
        edit.setRange(low, high)
        edit.setDecimals(4)
        edit.setValue(getattr(self.service.config, name))
        edit.setObjectName(name)
        form.addRow(title, edit)
        self.fields[name] = edit

    def _check(self, form, name, title):
        edit = QCheckBox(title)
        edit.setChecked(getattr(self.service.config, name))
        edit.setObjectName(name)
        form.addRow(edit)
        self.fields[name] = edit

    def _combo(self, form, name, title, choices):
        edit = QComboBox()
        for caption, value in choices:
            edit.addItem(caption, value)
        index = edit.findData(getattr(self.service.config, name))
        if index >= 0:
            edit.setCurrentIndex(index)
        edit.setObjectName(name)
        form.addRow(title, edit)
        self.fields[name] = edit

    def values(self):
        values = {}
        list_fields = {"hashtags", "disabled_hashtags", "keywords", "categories"}
        for name, edit in self.fields.items():
            if isinstance(edit, QCheckBox):
                value = edit.isChecked()
            elif isinstance(edit, QComboBox):
                value = edit.currentData()
            elif isinstance(edit, (QSpinBox, QDoubleSpinBox)):
                value = edit.value()
            else:
                value = edit.toPlainText() if isinstance(edit, QPlainTextEdit) else edit.text()
                value = value.strip()
                if name in list_fields:
                    value = [line.strip() for line in value.splitlines() if line.strip()]
                elif name == "image_order":
                    value = [int(x.strip()) for x in value.split(",") if x.strip()]
                elif name in {"input_price_per_million", "output_price_per_million"}:
                    value = float(value) if value else None
                elif name == "reward_by_image":
                    value = {int(k.strip()): float(v.strip()) for k, v in (part.split(":", 1) for part in value.split(",") if part.strip())}
            values[name] = value
        return values

    def _save(self):
        try:
            config = replace(self.service.config, **self.values())
            config._base_dir = self.service.config.base_dir
            self.service.save_settings(config)
        except Exception as exc:
            QMessageBox.warning(self, "Настройки не сохранены", str(exc))
            return
        self.accept()
