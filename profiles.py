"""User-managed TikTok identities and explicit, ordinary browser launches.

This module never inspects browser data, cookies, passwords or login state.
Opening a browser profile is not proof that TikTok is authenticated there.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import webbrowser

from validators import canonical_tiktok_url


_BROWSER_NAMES = {
    'chrome': {'chrome.exe', 'chrome', 'google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'},
    'edge': {'msedge.exe', 'msedge', 'microsoft-edge', 'microsoft-edge-stable'},
}


class ProfileManager:
    def __init__(self, store, db):
        self.store, self.db = store, db

    def list(self):
        result = []
        for profile in self.store.list_profiles():
            tasks = self.db.list_tasks(profile['profile_id'])
            result.append({**profile,
                           'drafts': sum(bool(task.get('comment')) for task in tasks),
                           'published': sum(task['publication_status'] == 'PUBLISHED' for task in tasks),
                           'auth_status': 'Вход не подтверждён программой',
                           'browser_warning': (
                               'Перед публикацией проверьте выбранный аккаунт в TikTok: отдельный профиль браузера не настроен.'
                               if profile['browser_kind'] == 'default' or not profile['browser_profile'] else '')})
        return result

    def save(self, slot, username='', browser_kind='default', browser_path='', browser_profile=''):
        if browser_kind not in ('default', 'chrome', 'edge'):
            raise ValueError('Выберите браузер из списка')
        if browser_path:
            self._validate_executable(browser_kind, browser_path)
        if browser_profile and not re.fullmatch(r'[A-Za-z0-9А-Яа-яЁё _-]{1,100}', browser_profile):
            raise ValueError('Укажите имя профиля браузера, например «Profile 1», без пути и команд')
        self.store.save_profile(slot, username=username, browser_kind=browser_kind,
                                browser_path=browser_path, browser_profile=browser_profile)
        return next(profile for profile in self.list() if profile['slot'] == slot)

    @staticmethod
    def _validate_executable(kind, value):
        if kind not in _BROWSER_NAMES:
            raise ValueError('Для отдельного профиля выберите Chrome или Edge')
        path = Path(value).expanduser()
        if (not path.is_file() or path.name.casefold() not in _BROWSER_NAMES[kind]
                or (os.name != 'nt' and not os.access(path, os.X_OK))):
            raise ValueError('Выберите настоящий исполняемый файл выбранного браузера Chrome или Edge')
        return str(path.resolve())

    def _executable(self, profile):
        kind = profile['browser_kind']
        if profile['browser_path']:
            return self._validate_executable(kind, profile['browser_path'])
        for name in sorted(_BROWSER_NAMES[kind]):
            executable = shutil.which(name)
            if executable:
                return self._validate_executable(kind, executable)
        if os.name == 'nt':
            relative = ('Google/Chrome/Application/chrome.exe' if kind == 'chrome'
                        else 'Microsoft/Edge/Application/msedge.exe')
            for variable in ('LOCALAPPDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)'):
                base = os.environ.get(variable)
                if base:
                    candidate = Path(base) / relative
                    if candidate.is_file():
                        return self._validate_executable(kind, candidate)
        raise ValueError('Браузер не найден. Укажите путь к Chrome или Edge в разделе «Аккаунты».')

    def _open(self, profile, url):
        if profile['browser_kind'] == 'default':
            if not webbrowser.open(url, new=2):
                raise ValueError('Не удалось открыть браузер. Откройте ссылку вручную.')
            return True
        executable = self._executable(profile)
        command = [executable]
        browser_profile = profile.get('browser_profile') or ''
        if browser_profile:
            if not re.fullmatch(r'[A-Za-z0-9А-Яа-яЁё _-]{1,100}', browser_profile):
                raise ValueError('Небезопасное имя профиля браузера')
            command.append('--profile-directory=' + browser_profile)
        command.append(url)
        try:
            subprocess.Popen(command, shell=False)
        except OSError:
            raise ValueError('Не удалось запустить браузер. Проверьте путь и имя профиля.') from None
        return True

    def open_video(self, profile_id, url):
        profile = self.store.get_profile(profile_id)
        canonical, _ = canonical_tiktok_url(url)
        return self._open(profile, canonical)

    def open_profile(self, profile_id):
        profile = self.store.get_profile(profile_id)
        username = profile.get('username') or ''
        if username and not re.fullmatch(r'[A-Za-z0-9_.]{1,24}', username):
            raise ValueError('Некорректное имя TikTok')
        url = 'https://www.tiktok.com/@' + username if username else 'https://www.tiktok.com/'
        return self._open(profile, url)
