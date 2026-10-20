"""Small atomic, non-secret desktop preferences; independent of task history."""
from __future__ import annotations

import json
from pathlib import Path
import threading

from credentials import _atomic_write


class UserState:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _read(self):
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                raise ValueError
            return data
        except Exception:
            raise ValueError('Не удалось прочитать настройки первого запуска; файл сохранён без изменений') from None

    def get(self, key, default=None):
        with self._lock:
            return self._read().get(key, default)

    def set(self, key, value):
        if key not in {'onboarding_complete'} or type(value) is not bool:
            raise ValueError('Неизвестная настройка первого запуска')
        with self._lock:
            data = self._read()
            data[key] = value
            try:
                _atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))
            except Exception:
                raise ValueError('Не удалось сохранить настройки первого запуска') from None
