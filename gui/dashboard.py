"""Compact, honest application statistics."""
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QVBoxLayout, QWidget


class Dashboard(QWidget):
    METRICS = (
        ("videos", "Видео в базе"), ("processed", "Обработано задач"),
        ("drafts", "Черновиков"), ("published", "Публикаций подтверждено"),
        ("errors", "Ошибок"), ("telegram_sent", "Ссылок отправлено"),
        ("total_cost", "Расходы OpenAI, $"), ("confirmed_reward", "Вознаграждение подтверждено"),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.labels = {}
        for i, (key, title) in enumerate(self.METRICS):
            card = QFrame()
            card.setObjectName("metricCard")
            box = QVBoxLayout(card)
            label = QLabel("—")
            label.setObjectName("metricValue")
            caption = QLabel(title)
            caption.setWordWrap(True)
            caption.setObjectName("muted")
            box.addWidget(label)
            box.addWidget(caption)
            layout.addWidget(card, i // 4, i % 4)
            self.labels[key] = label

    def set_stats(self, stats):
        for key, label in self.labels.items():
            value = stats.get(key)
            if value is None:
                label.setText("не рассчитано" if key == "total_cost" else "0")
            elif key in {"total_cost", "confirmed_reward"}:
                label.setText(f"{value:,.4f}".rstrip("0").rstrip("."))
            else:
                label.setText(str(value))
