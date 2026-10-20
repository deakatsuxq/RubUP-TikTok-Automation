"""Three everyday indicators; detailed accounting belongs to the history page."""
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget


class Dashboard(QWidget):
    METRICS = (("processed", "Обработано видео"), ("drafts", "Готово комментариев"),
               ("telegram_sent", "Отправлено ссылок"))

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        self.labels = {}
        for key, caption in self.METRICS:
            card = QFrame()
            card.setObjectName("metricCard")
            box = QVBoxLayout(card)
            box.setContentsMargins(18, 14, 18, 14)
            label = QLabel("0")
            label.setObjectName("metricValue")
            title = QLabel(caption)
            title.setObjectName("muted")
            box.addWidget(label)
            box.addWidget(title)
            layout.addWidget(card, 1)
            self.labels[key] = label
        # Detailed confirmation counts are deliberately outside the home page.
        self.details_widget = QWidget()
        details = QHBoxLayout(self.details_widget)
        details.setContentsMargins(0, 0, 0, 0)
        for key, caption in (('published', 'Подтверждено публикаций'), ('errors', 'Ошибок обработки')):
            card = QFrame()
            card.setObjectName('metricCard')
            box = QVBoxLayout(card)
            label = QLabel('0')
            label.setObjectName('metricValue')
            box.addWidget(label)
            title = QLabel(caption)
            title.setObjectName('muted')
            box.addWidget(title)
            details.addWidget(card)
            self.labels[key] = label

    def set_stats(self, stats):
        for key, label in self.labels.items():
            label.setText(str(stats.get(key, 0) or 0))
        self.setToolTip(f"Опубликовано с подтверждением пользователя: {stats.get('published', 0)}")
