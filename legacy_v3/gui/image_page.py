"""Four original image cards and understandable file-selection preferences."""
from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)


class ImagePage(QWidget):
    changed = Signal()
    failed = Signal(str)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.previews, self.descriptions, self.buttons = {}, {}, {}
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Картинки")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        hint = QLabel("Добавьте свои четыре оригинала. Качество и размеры файлов сохраняются.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        root.addWidget(hint)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content.setObjectName("pageContent")
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 12, 8, 0)
        grid = QGridLayout()
        grid.setSpacing(16)
        for slot in range(1, 5):
            card = QFrame()
            card.setObjectName("imageCard")
            box = QVBoxLayout(card)
            box.setContentsMargins(20, 18, 20, 18)
            box.setSpacing(12)
            caption = QLabel(f"Картинка №{slot}")
            caption.setObjectName("cardTitle")
            box.addWidget(caption)
            preview = QLabel("Оригинал пока не добавлен")
            preview.setObjectName("imagePreview")
            preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            preview.setMinimumHeight(150)
            box.addWidget(preview)
            details = QLabel()
            details.setObjectName("muted")
            details.setWordWrap(True)
            box.addWidget(details)
            button = QPushButton("Добавить")
            button.clicked.connect(lambda checked=False, number=slot: self.choose_image(number))
            box.addWidget(button)
            self.previews[slot], self.descriptions[slot], self.buttons[slot] = preview, details, button
            grid.addWidget(card, (slot - 1) // 2, (slot - 1) % 2)
        body.addLayout(grid)
        preferences = QFrame()
        preferences.setObjectName("panel")
        form = QFormLayout(preferences)
        form.setContentsMargins(20, 18, 20, 18)
        form.setSpacing(12)
        self.mode_combo = QComboBox()
        for text, value in (
            ("Без картинок", "none"), ("Чередовать картинки", "rotate"),
            ("Выбрать конкретную", "specific"), ("По требованиям задания", "task"),
        ):
            self.mode_combo.addItem(text, value)
        self.mode_combo.setToolTip("Картинка выбирается только для заданий, которым она нужна. Прикрепление в TikTok выполняете вы.")
        form.addRow("Как выбирать картинку", self.mode_combo)
        self.specific_combo = QComboBox()
        for number in range(1, 5):
            self.specific_combo.addItem(f"Картинка №{number}", number)
        form.addRow("Конкретная картинка", self.specific_combo)
        self.order_edit = QLineEdit()
        self.order_edit.setPlaceholderText("1,2,3,4")
        self.order_edit.setToolTip("Все четыре номера в нужном порядке, без повторов")
        form.addRow("Порядок чередования", self.order_edit)
        self.apply_button = QPushButton("Сохранить выбор")
        self.apply_button.setObjectName("secondary")
        self.apply_button.clicked.connect(self.apply_settings)
        form.addRow(self.apply_button)
        body.addWidget(preferences)
        note = QLabel("Режим определяет, какой файл использовать, если задание требует картинку. Приложение покажет нужный оригинал; прикрепите его к комментарию вручную.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        body.addWidget(note)
        self.feedback = QLabel()
        self.feedback.setWordWrap(True)
        self.feedback.setObjectName("successText")
        body.addWidget(self.feedback)
        body.addStretch()
        area.setWidget(content)
        root.addWidget(area, 1)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self.reload()

    def _mode_changed(self):
        mode = self.mode_combo.currentData()
        self.specific_combo.setEnabled(mode == "specific")
        self.order_edit.setEnabled(mode == "rotate")

    def choose_image(self, slot):
        path, _ = QFileDialog.getOpenFileName(self, f"Добавить картинку №{slot}", "", "Картинки (*.jpg *.jpeg *.png)")
        if path:
            self.add_image(slot, path)

    def add_image(self, slot, path):
        """Import exact original bytes; useful for both picker and setup wizard."""
        try:
            self.service.images.set_image(slot, path)
            self.feedback.setText(f"Картинка №{slot} сохранена в исходном качестве")
            self.reload()
            self.changed.emit()
            return True
        except (ValueError, OSError) as exc:
            self.failed.emit(str(exc))
            return False

    def apply_settings(self):
        try:
            try:
                order = [int(value.strip()) for value in self.order_edit.text().split(",") if value.strip()]
            except ValueError:
                raise ValueError("Укажите четыре номера через запятую, например 1,2,3,4") from None
            if len(order) != 4 or set(order) != {1, 2, 3, 4}:
                raise ValueError("В порядке картинок должны быть все четыре номера без повторов")
            config = replace(self.service.config, image_mode=self.mode_combo.currentData(),
                             selected_image=self.specific_combo.currentData(), image_order=order)
            if config.image_mode == "none":
                config.require_image = False
            result = self.service.save_settings(config)
            self.feedback.setText("Выбор сохранён. Перезапустите программу для применения." if result.get("restart_required") else "Выбор сохранён и применяется к новым заданиям")
            self.changed.emit()
            return True
        except (ValueError, OSError, RuntimeError) as exc:
            self.failed.emit(str(exc))
            return False

    def reload(self):
        config = self.service.config
        self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(config.image_mode)))
        self.specific_combo.setCurrentIndex(max(0, self.specific_combo.findData(config.selected_image)))
        self.order_edit.setText(",".join(str(number) for number in config.image_order))
        self._mode_changed()
        for slot in range(1, 5):
            preview, description = self.previews[slot], self.descriptions[slot]
            try:
                image = self.service.images.get_image(slot)
                preview.clear()
                if image is None:
                    preview.setText("Оригинал пока не добавлен")
                    description.setText("Выберите свой файл JPG или PNG")
                    self.buttons[slot].setText("Добавить")
                    continue
                valid = self.service.images.verify(slot)
                if valid:
                    pixmap = QPixmap(image["path"])
                    preview.setPixmap(pixmap.scaled(350, 150, Qt.AspectRatioMode.KeepAspectRatio,
                                                   Qt.TransformationMode.SmoothTransformation))
                    description.setText(f"Файл готов · {image['width']} × {image['height']} · качество сохранено")
                else:
                    preview.setText("Нужно проверить файл")
                    description.setText("Оригинал изменён или повреждён. Добавьте исходный файл ещё раз.")
                self.buttons[slot].setText("Заменить")
            except (ValueError, OSError) as exc:
                preview.setText("Не удалось прочитать файл")
                description.setText(str(exc))

    def set_busy(self, busy):
        self.apply_button.setEnabled(not busy)
        self.mode_combo.setEnabled(not busy)
        for button in self.buttons.values():
            button.setEnabled(not busy)
        self.specific_combo.setEnabled(not busy and self.mode_combo.currentData() == "specific")
        self.order_edit.setEnabled(not busy and self.mode_combo.currentData() == "rotate")
