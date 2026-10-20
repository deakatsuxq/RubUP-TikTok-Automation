"""Local credential persistence without changing the process environment.

Windows uses user-scoped DPAPI encryption. Other platforms retain a private,
atomically written .env file for compatibility with the original application.
No secret is ever part of an exception or a log message in this module.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import io
import os
from pathlib import Path
import tempfile
import threading

from dotenv import dotenv_values
from dotenv.parser import parse_stream


CREDENTIAL_NAMES = ('OPENAI_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID')
_MAGIC = b'RUBUP-DPAPI1\0'


class CredentialError(ValueError):
    """A safe error suitable for display; underlying secret data is omitted."""


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.rubup-private-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class _DPAPIProtector:
    """The Windows current-user key encrypts and authenticates credential bytes."""

    class _Blob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]

    def _transform(self, data: bytes, encrypt: bool) -> bytes:
        if os.name != 'nt':
            raise CredentialError('Защищённое хранилище Windows недоступно на этой системе')
        try:
            crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            buffer = ctypes.create_string_buffer(data)
            incoming = self._Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
            outgoing = self._Blob()
            crypt32.CryptProtectData.argtypes = [
                ctypes.POINTER(self._Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                ctypes.POINTER(self._Blob),
            ]
            crypt32.CryptProtectData.restype = wintypes.BOOL
            crypt32.CryptUnprotectData.argtypes = [
                ctypes.POINTER(self._Blob), ctypes.c_void_p, ctypes.c_void_p,
                ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                ctypes.POINTER(self._Blob),
            ]
            crypt32.CryptUnprotectData.restype = wintypes.BOOL
            kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            kernel32.LocalFree.restype = ctypes.c_void_p
            if encrypt:
                ok = crypt32.CryptProtectData(ctypes.byref(incoming), 'RubUP Automation',
                                             None, None, None, 1, ctypes.byref(outgoing))
            else:
                ok = crypt32.CryptUnprotectData(ctypes.byref(incoming), None,
                                               None, None, None, 1, ctypes.byref(outgoing))
            if not ok:
                raise CredentialError('Windows не смогла открыть защищённое хранилище этого пользователя')
            try:
                return ctypes.string_at(outgoing.pbData, outgoing.cbData)
            finally:
                if outgoing.pbData:
                    kernel32.LocalFree(ctypes.cast(outgoing.pbData, ctypes.c_void_p))
        except CredentialError:
            raise
        except Exception:
            raise CredentialError('Не удалось использовать защищённое хранилище Windows') from None

    def protect(self, raw: bytes) -> bytes:
        return self._transform(raw, True)

    def unprotect(self, raw: bytes) -> bytes:
        return self._transform(raw, False)


class CredentialStore:
    """Explicit local settings override legacy .env and inherited bindings.

    A saved empty string is intentional: removing a key in the UI must not
    silently reactivate an older key inherited from a shell. Nothing here writes
    or deletes exported environment variables of this or another application.
    """

    def __init__(self, project_root: str | Path, *, windows=None, protector=None, environ=None):
        self.root = Path(project_root).resolve()
        self.env_path = self.root / '.env'
        self.private_path = self.root / 'data' / 'credentials.dpapi'
        self.windows = os.name == 'nt' if windows is None else bool(windows)
        self.protector = protector or _DPAPIProtector()
        self.environ = os.environ if environ is None else environ
        self._lock = threading.RLock()

    @property
    def storage_label(self) -> str:
        return 'Защищённое хранилище Windows' if self.windows else 'Локальный файл .env (доступ владельцу)'

    @staticmethod
    def _validate(values):
        if not isinstance(values, dict) or set(values) - set(CREDENTIAL_NAMES):
            raise CredentialError('Переданы неизвестные параметры подключения')
        result = {}
        for name, value in values.items():
            if value is None:
                continue
            if not isinstance(value, str) or len(value) > 8192 or any(c in value for c in ('\r', '\n', '\0')):
                raise CredentialError('Значение подключения должно быть одной строкой')
            result[name] = value.strip()
        return result

    def _legacy(self):
        try:
            values = dotenv_values(self.env_path, interpolate=False) if self.env_path.is_file() else {}
            return self._validate({name: value if value is not None else '' for name, value in values.items()
                                   if name in CREDENTIAL_NAMES})
        except Exception:
            raise CredentialError('Не удалось прочитать локальные настройки подключения') from None

    def _private(self):
        if not self.private_path.exists():
            return {}
        try:
            raw = self.private_path.read_bytes()
            if not raw.startswith(_MAGIC):
                raise ValueError
            decoded = self.protector.unprotect(raw[len(_MAGIC):])
            data = json.loads(decoded.decode('utf-8'))
            return self._validate(data)
        except Exception:
            raise CredentialError('Не удалось открыть защищённые настройки. Используйте того же пользователя Windows или восстановите резервную копию') from None

    def get_all(self) -> dict[str, str]:
        with self._lock:
            result = {name: str(self.environ.get(name) or '') for name in CREDENTIAL_NAMES}
            result.update(self._legacy())
            if self.windows:
                result.update(self._private())
            return self._validate(result)

    def get(self, name: str, default='') -> str:
        if name not in CREDENTIAL_NAMES:
            raise CredentialError('Неизвестный параметр подключения')
        return self.get_all().get(name, default)

    @staticmethod
    def _env_text(previous: str, values: dict[str, str]) -> str:
        def quoted(value):
            return "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'"
        remaining = set(values)
        lines = []
        for binding in parse_stream(io.StringIO(previous)):
            line, key = binding.original.string, binding.key
            if key not in values:
                lines.append(line)
            elif key in remaining:
                lines.append(f'{key}={quoted(values[key])}\n')
                remaining.remove(key)
            # Duplicate assignments of a changed key are intentionally removed.
        for key in CREDENTIAL_NAMES:
            if key in remaining:
                if lines and not lines[-1].endswith(('\n', '\r')):
                    lines[-1] += '\n'
                lines.append(f'{key}={quoted(values[key])}\n')
        return ''.join(lines)

    def update(self, values: dict[str, str | None]) -> None:
        values = self._validate(values)
        if not values:
            return
        with self._lock:
            try:
                if self.windows:
                    existing = self._private()
                    # The first encrypted save migrates credential bindings from
                    # the legacy file without removing unrelated user settings.
                    if not self.private_path.exists():
                        existing.update(self._legacy())
                    existing.update(values)
                    raw = json.dumps(existing, ensure_ascii=False).encode('utf-8')
                    protected = self.protector.protect(raw)
                    if not isinstance(protected, bytes) or not protected:
                        raise CredentialError('Windows не смогла сохранить настройки безопасно')
                    _atomic_write(self.private_path, _MAGIC + protected)
                    # No plaintext fallback on Windows if DPAPI fails. Existing
                    # .env is left unchanged so the upgrade never deletes it.
                else:
                    previous = self.env_path.read_text(encoding='utf-8-sig') if self.env_path.exists() else ''
                    _atomic_write(self.env_path, self._env_text(previous, values).encode('utf-8'))
            except CredentialError:
                raise
            except Exception:
                raise CredentialError('Не удалось сохранить настройки подключения. Проверьте права на папку программы') from None
