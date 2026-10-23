"""Two local account labels and explicit user-managed browser profiles."""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget


class AccountsPage(QWidget):
    changed = Signal()
    failed = Signal(str)

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.manager, self.fields, self.controls = manager, {}, []
        self._dirty = set()
        root = QVBoxLayout(self)
        title = QLabel('Мои аккаунты'); title.setObjectName('pageTitle'); root.addWidget(title)
        hint = QLabel('Войдите в TikTok самостоятельно. Программа не читает пароли и cookies и не может проверить, кто вошёл в браузере.')
        hint.setWordWrap(True); hint.setObjectName('muted'); root.addWidget(hint)
        cards = QHBoxLayout()
        for slot in (1, 2):
            card = QFrame(); card.setObjectName('card'); box = QVBoxLayout(card); box.setContentsMargins(22, 22, 22, 22); box.setSpacing(15)
            title = QLabel(f'Аккаунт №{slot}'); title.setObjectName('sectionTitle'); box.addWidget(title)
            status = QLabel('Вход не подтверждён программой'); status.setObjectName('warning'); status.setWordWrap(True); box.addWidget(status)
            form = QFormLayout(); username = QLineEdit(); username.setPlaceholderText('Имя без @'); form.addRow('Имя в TikTok', username)
            browser = QComboBox()
            for text, value in [('Браузер по умолчанию', 'default'), ('Google Chrome', 'chrome'), ('Microsoft Edge', 'edge')]: browser.addItem(text, value)
            form.addRow('Браузер', browser)
            path = QLineEdit(); path.setPlaceholderText('Путь к chrome.exe или msedge.exe')
            row = QHBoxLayout(); row.addWidget(path); browse = QPushButton('Выбрать…'); row.addWidget(browse)
            browse.clicked.connect(lambda checked=False, edit=path: self.choose_browser(edit)); form.addRow('Файл браузера', row)
            profile = QLineEdit(); profile.setPlaceholderText('Например: Profile 1'); profile.setToolTip('Имя уже созданного профиля Chrome/Edge, без команд и путей.')
            form.addRow('Профиль браузера ?', profile); box.addLayout(form)
            note = QLabel('Создайте два разных профиля в самом браузере и войдите в нужный аккаунт в каждом. Перед публикацией проверьте имя в TikTok.')
            note.setWordWrap(True); note.setObjectName('muted'); box.addWidget(note)
            stats = QLabel(); stats.setWordWrap(True); stats.setObjectName('muted'); box.addWidget(stats)
            save = QPushButton('Сохранить аккаунт'); save.clicked.connect(lambda checked=False, s=slot: self.save(s)); box.addWidget(save)
            opening = QPushButton('Открыть TikTok'); opening.clicked.connect(lambda checked=False, s=slot: self.open_profile(s)); box.addWidget(opening); box.addStretch()
            self.fields[slot] = dict(username=username, browser_kind=browser, browser_path=path, browser_profile=profile, status=status, stats=stats)
            self.controls.extend([username, browser, path, profile, browse, save, opening]); cards.addWidget(card, 1)
            browser.currentIndexChanged.connect(lambda _, s=slot: self.browser_changed(s))
            browser.currentIndexChanged.connect(lambda _, s=slot: self._dirty.add(s))
            for field in (username, path, profile): field.textChanged.connect(lambda _, s=slot: self._dirty.add(s))
        root.addLayout(cards, 1); self.feedback = QLabel(); self.feedback.setWordWrap(True); root.addWidget(self.feedback); self.reload()

    def choose_browser(self, field):
        path, _ = QFileDialog.getOpenFileName(self, 'Выберите браузер', '', 'Программы (*.exe);;Все файлы (*)')
        if path: field.setText(path)

    def browser_changed(self, slot):
        fields = self.fields[slot]; custom = fields['browser_kind'].currentData() != 'default'
        fields['browser_path'].setEnabled(custom); fields['browser_profile'].setEnabled(custom)

    def reload(self):
        try:
            self.profiles = self.manager.list()
            for profile in self.profiles:
                slot = profile['slot']; fields = self.fields[slot]
                if slot not in self._dirty:
                    for name in ('username', 'browser_path', 'browser_profile'):
                        fields[name].blockSignals(True); fields[name].setText(profile.get(name) or ''); fields[name].blockSignals(False)
                    fields['browser_kind'].blockSignals(True)
                    fields['browser_kind'].setCurrentIndex(max(0, fields['browser_kind'].findData(profile.get('browser_kind', 'default'))))
                    fields['browser_kind'].blockSignals(False)
                fields['stats'].setText(f"Готово комментариев: {profile.get('drafts', 0)}\nПодтверждено публикаций: {profile.get('published', 0)}" + ('\n' + profile['browser_warning'] if profile.get('browser_warning') else ''))
                self.browser_changed(slot)
        except Exception as exc: self.failed.emit(str(exc))

    def save(self, slot):
        try:
            fields = self.fields[slot]
            self.manager.save(slot, username=fields['username'].text().strip().lstrip('@'), browser_kind=fields['browser_kind'].currentData(), browser_path=fields['browser_path'].text().strip(), browser_profile=fields['browser_profile'].text().strip())
            self._dirty.discard(slot)
            self.feedback.setText('Локальный профиль сохранён. Авторизацию проверьте самостоятельно в браузере.')
            self.reload(); self.changed.emit(); return True
        except Exception as exc: self.failed.emit(str(exc)); return False

    def open_profile(self, slot):
        try:
            profile = next(p for p in self.manager.list() if p['slot'] == slot)
            return self.manager.open_profile(profile['profile_id'])
        except Exception as exc: self.failed.emit(str(exc)); return False

    def set_busy(self, busy):
        for widget in self.controls: widget.setEnabled(not busy)
        if not busy:
            for slot in (1, 2): self.browser_changed(slot)
