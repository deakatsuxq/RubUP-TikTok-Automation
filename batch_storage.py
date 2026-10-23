"""Additive, durable storage for two-profile preparation sessions.

The original SQLite tables and their version are deliberately left alone. A
separate version marker owns these additions and an existing on-disk database
is copied with SQLite's backup API before the first migration.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import uuid

from scheduler import ProcessLock


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class BatchStore:
    BATCH_STATES = {'IMPORTING', 'QUEUED', 'PREPARING', 'PAUSED', 'STOPPED', 'READY', 'FAILED'}
    ITEM_STATES = {'QUEUED', 'RESOLVED', 'READY', 'FAILED', 'DUPLICATE', 'SKIPPED'}
    QUEUE_STATES = {'PENDING', 'FAILED', 'IN_FLIGHT', 'SENT', 'UNKNOWN'}
    PROFILE_FIELDS = {'username', 'browser_kind', 'browser_path', 'browser_profile'}

    def __init__(self, db):
        self.db = db
        self.backup_path = None
        self._migrate()

    def _migrate(self):
        lock = None
        if not getattr(self.db, '_uri', False):
            lock = ProcessLock(str(self.db.path) + '.v3-migration.lock')
            if not lock.acquire():
                raise ValueError('Другое окно программы обновляет базу. Повторите позже.')
        try:
            with self.db._lock:
                with self.db._connection() as conn:
                    table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='batch_schema'").fetchone()
                    row = conn.execute('SELECT version FROM batch_schema WHERE singleton=1').fetchone() if table else None
                    if row:
                        if row['version'] != 3:
                            raise ValueError('Версия пакетной базы не поддерживается. Файл сохранён без изменений.')
                        return
                # Database has already initialized the v2 schema. Even an empty
                # on-disk v2 file gets a recoverable copy before adding tables.
                if not getattr(self.db, '_uri', False):
                    self.backup_path = self._backup()
                statements = [
                    '''CREATE TABLE IF NOT EXISTS batch_schema (
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL)''',
                    '''CREATE TABLE IF NOT EXISTS batch_profiles (
                        profile_id TEXT PRIMARY KEY, slot INTEGER NOT NULL UNIQUE CHECK(slot IN (1,2)),
                        username TEXT NOT NULL DEFAULT '', browser_kind TEXT NOT NULL DEFAULT 'default'
                            CHECK(browser_kind IN ('default','chrome','edge')),
                        browser_path TEXT NOT NULL DEFAULT '', browser_profile TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''',
                    '''CREATE TABLE IF NOT EXISTS batches (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, profile_ids TEXT NOT NULL,
                        state TEXT NOT NULL DEFAULT 'QUEUED'
                            CHECK(state IN ('IMPORTING','QUEUED','PREPARING','PAUSED','STOPPED','READY','FAILED')),
                        limit_usd REAL CHECK(limit_usd >= 0),
                        is_test INTEGER NOT NULL DEFAULT 0 CHECK(is_test IN (0,1)),
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''',
                    '''CREATE TABLE IF NOT EXISTS batch_items (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL REFERENCES batches(id),
                        position INTEGER NOT NULL CHECK(position >= 1), source_url TEXT NOT NULL,
                        video_id TEXT REFERENCES videos(video_id), state TEXT NOT NULL DEFAULT 'QUEUED'
                            CHECK(state IN ('QUEUED','RESOLVED','READY','FAILED','DUPLICATE','SKIPPED')),
                        error TEXT NOT NULL DEFAULT '',
                        metadata_attempted INTEGER NOT NULL DEFAULT 0 CHECK(metadata_attempted IN (0,1)),
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                        UNIQUE(batch_id,source_url), UNIQUE(batch_id,position))''',
                    'CREATE INDEX IF NOT EXISTS batch_items_queue ON batch_items(batch_id,state,position)',
                    'CREATE INDEX IF NOT EXISTS batch_items_video ON batch_items(batch_id,video_id)',
                    '''CREATE TABLE IF NOT EXISTS batch_telegram_queue (
                        url TEXT PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos(video_id),
                        batch_id INTEGER NOT NULL REFERENCES batches(id),
                        status TEXT NOT NULL DEFAULT 'PENDING'
                            CHECK(status IN ('PENDING','FAILED','IN_FLIGHT','SENT','UNKNOWN')),
                        error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''',
                    'CREATE INDEX IF NOT EXISTS batch_telegram_pending ON batch_telegram_queue(status,batch_id)',
                ]
                with self.db._connection(write=True) as conn:
                    for statement in statements:
                        conn.execute(statement)
                    conn.execute('INSERT INTO batch_schema(singleton,version) VALUES (1,3) ON CONFLICT(singleton) DO NOTHING')
        finally:
            if lock:
                lock.release()

    def _backup(self):
        source = Path(self.db.path).resolve()
        directory = source.parent / 'backups'
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
        destination = directory / f'{source.name}.pre-v3.{uuid.uuid4().hex}.sqlite3'
        try:
            with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
                src.backup(dst)
                if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise sqlite3.DatabaseError('backup integrity')
            destination.chmod(0o600)
        except Exception:
            # Incomplete copies cannot be mistaken for a recoverable backup.
            destination.unlink(missing_ok=True)
            raise ValueError('Не удалось создать резервную копию базы. Обновление не выполнено.') from None
        return destination

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        result = dict(row)
        if 'profile_ids' in result:
            result['profile_ids'] = json.loads(result['profile_ids'])
        for field in ('is_test', 'metadata_attempted'):
            if field in result:
                result[field] = bool(result[field])
        return result

    def ensure_profiles(self, legacy_account_id):
        if not isinstance(legacy_account_id, str) or not legacy_account_id.strip():
            raise ValueError('Не указан идентификатор первого аккаунта')
        with self.db._connection(write=True) as conn:
            now = _now()
            conn.execute('''INSERT INTO batch_profiles(profile_id,slot,created_at,updated_at)
                VALUES (?,1,?,?) ON CONFLICT(slot) DO NOTHING''', (legacy_account_id, now, now))
            conn.execute('''INSERT INTO batch_profiles(profile_id,slot,created_at,updated_at)
                VALUES (?,2,?,?) ON CONFLICT(slot) DO NOTHING''', ('account-2-' + uuid.uuid4().hex, now, now))
        return self.list_profiles()

    def list_profiles(self):
        with self.db._connection() as conn:
            return [self._decode(row) for row in conn.execute('SELECT * FROM batch_profiles ORDER BY slot')]

    def get_profile(self, profile_id):
        with self.db._connection() as conn:
            row = conn.execute('SELECT * FROM batch_profiles WHERE profile_id=?', (str(profile_id),)).fetchone()
            if row is None:
                raise KeyError('Аккаунт не найден')
            return self._decode(row)

    def save_profile(self, slot, **fields):
        if type(slot) is not int or slot not in (1, 2):
            raise ValueError('Выберите аккаунт №1 или №2')
        if set(fields) - self.PROFILE_FIELDS:
            raise ValueError('Идентификатор аккаунта и историю нельзя изменить')
        if any(not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value)
               for value in fields.values()):
            raise ValueError('Настройки аккаунта должны быть обычным текстом')
        fields = {key: value.strip() for key, value in fields.items()}
        if 'username' in fields:
            fields['username'] = fields['username'].removeprefix('@')
            if fields['username'] and not re.fullmatch(r'[A-Za-z0-9_.]{1,24}', fields['username']):
                raise ValueError('Имя TikTok: до 24 латинских букв, цифр, точек и подчёркиваний')
        if fields.get('browser_kind', 'default') not in ('default', 'chrome', 'edge'):
            raise ValueError('Выберите браузер из списка')
        if fields.get('browser_profile') and not re.fullmatch(r'[A-Za-z0-9А-Яа-яЁё _-]{1,100}', fields['browser_profile']):
            raise ValueError('Укажите имя профиля браузера без пути и команд')
        with self.db._connection(write=True) as conn:
            current = conn.execute('SELECT * FROM batch_profiles WHERE slot=?', (slot,)).fetchone()
            if not current:
                raise KeyError('Сначала создайте два аккаунта')
            other = conn.execute('SELECT * FROM batch_profiles WHERE slot<>?', (slot,)).fetchone()
            if fields.get('username'):
                if other and other['username'].casefold() == fields['username'].casefold():
                    raise ValueError('Укажите два разных TikTok-аккаунта')
            candidate = {**dict(current), **fields}
            if other and candidate['browser_kind'] != 'default' and candidate['browser_profile']:
                def browser_tuple(profile):
                    path = str(Path(profile['browser_path']).expanduser().resolve()) if profile['browser_path'] else ''
                    return (profile['browser_kind'], path.casefold(), profile['browser_profile'].casefold())
                if browser_tuple(candidate) == browser_tuple(dict(other)):
                    raise ValueError('Выберите разные профили браузера для двух аккаунтов TikTok')
            if fields:
                fields['updated_at'] = _now()
                conn.execute(f"UPDATE batch_profiles SET {','.join(key+'=?' for key in fields)} WHERE slot=?",
                             [*fields.values(), slot])
            return self._decode(conn.execute('SELECT * FROM batch_profiles WHERE slot=?', (slot,)).fetchone())

    def create_batch(self, profile_ids, limit_usd=None, is_test=False):
        if (not isinstance(profile_ids, list) or len(profile_ids) != 2
                or not all(isinstance(p, str) and p for p in profile_ids) or len(set(profile_ids)) != 2):
            raise ValueError('Для партии нужны два разных аккаунта')
        if limit_usd is not None and (isinstance(limit_usd, bool) or not isinstance(limit_usd, (int, float))
                                      or not math.isfinite(limit_usd) or limit_usd < 0):
            raise ValueError('Лимит расходов должен быть конечным неотрицательным числом')
        if type(is_test) is not bool:
            raise ValueError('Неверный режим партии')
        with self.db._connection(write=True) as conn:
            for profile_id in profile_ids:
                if not conn.execute('SELECT 1 FROM batch_profiles WHERE profile_id=?', (profile_id,)).fetchone():
                    raise ValueError('Один из аккаунтов не найден')
            now = _now()
            cur = conn.execute('''INSERT INTO batches(profile_ids,limit_usd,is_test,created_at,updated_at)
                VALUES (?,?,?,?,?)''', (json.dumps(profile_ids), limit_usd, int(is_test), now, now))
            batch_id = cur.lastrowid
        return self.get_batch(batch_id)

    def get_batch(self, batch_id):
        with self.db._connection() as conn:
            row = conn.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
            if row is None:
                raise KeyError('Партия не найдена')
            return self._decode(row)

    def list_batches(self):
        with self.db._connection() as conn:
            return [self._decode(row) for row in conn.execute('SELECT * FROM batches ORDER BY id DESC')]

    def set_batch_state(self, batch_id, state):
        if not isinstance(state, str) or state.upper() not in self.BATCH_STATES:
            raise ValueError('Неизвестное состояние партии')
        with self.db._connection(write=True) as conn:
            cur = conn.execute('UPDATE batches SET state=?,updated_at=? WHERE id=?', (state.upper(), _now(), batch_id))
            if not cur.rowcount:
                raise KeyError('Партия не найдена')
        return self.get_batch(batch_id)

    def add_inputs(self, batch_id, urls):
        if not isinstance(urls, list) or len(urls) > 100:
            raise ValueError('Добавляйте не более 100 ссылок за раз')
        if any(not isinstance(url, str) or not url.strip() or len(url) > 2048 or any(ord(c) < 32 for c in url)
               for url in urls):
            raise ValueError('Некорректная строка ссылки')
        result = {'accepted': 0, 'duplicates': 0, 'invalid': 0, 'items': []}
        with self.db._connection(write=True) as conn:
            if not conn.execute('SELECT 1 FROM batches WHERE id=?', (batch_id,)).fetchone():
                raise KeyError('Партия не найдена')
            position = conn.execute('SELECT COALESCE(MAX(position),0) FROM batch_items WHERE batch_id=?', (batch_id,)).fetchone()[0]
            for value in urls:
                url = value.strip()
                if conn.execute('SELECT 1 FROM batch_items WHERE batch_id=? AND source_url=?', (batch_id, url)).fetchone():
                    result['duplicates'] += 1
                    continue
                position += 1
                now = _now()
                cur = conn.execute('''INSERT INTO batch_items(batch_id,position,source_url,created_at,updated_at)
                    VALUES (?,?,?,?,?)''', (batch_id, position, url, now, now))
                result['accepted'] += 1
                result['items'].append(self._decode(conn.execute('SELECT * FROM batch_items WHERE id=?', (cur.lastrowid,)).fetchone()))
        return result

    def list_items(self, batch_id):
        with self.db._connection() as conn:
            return [self._decode(row) for row in conn.execute('SELECT * FROM batch_items WHERE batch_id=? ORDER BY position', (batch_id,))]

    def get_item(self, item_id):
        with self.db._connection() as conn:
            row = conn.execute('SELECT * FROM batch_items WHERE id=?', (item_id,)).fetchone()
            if row is None:
                raise KeyError('Ссылка в очереди не найдена')
            return self._decode(row)

    def update_item(self, item_id, **fields):
        allowed = {'video_id', 'state', 'error', 'metadata_attempted'}
        if set(fields) - allowed:
            raise ValueError('Неизвестные параметры ссылки в очереди')
        if 'state' in fields:
            if not isinstance(fields['state'], str) or fields['state'].upper() not in self.ITEM_STATES:
                raise ValueError('Неизвестное состояние ссылки')
            fields['state'] = fields['state'].upper()
        if 'metadata_attempted' in fields:
            if type(fields['metadata_attempted']) is not bool:
                raise ValueError('Неверный признак проверки метаданных')
            fields['metadata_attempted'] = int(fields['metadata_attempted'])
        if 'error' in fields and not isinstance(fields['error'], str):
            raise ValueError('Ошибка должна быть текстом')
        with self.db._connection(write=True) as conn:
            if not conn.execute('SELECT 1 FROM batch_items WHERE id=?', (item_id,)).fetchone():
                raise KeyError('Ссылка в очереди не найдена')
            if fields:
                fields['updated_at'] = _now()
                conn.execute(f"UPDATE batch_items SET {','.join(key+'=?' for key in fields)} WHERE id=?", [*fields.values(), item_id])
        return self.get_item(item_id)

    def find_item(self, batch_id, video_id, exclude_id=None):
        query = 'SELECT * FROM batch_items WHERE batch_id=? AND video_id=? AND state<>?'
        args = [batch_id, str(video_id), 'DUPLICATE']
        if exclude_id is not None:
            query += ' AND id<>?'
            args.append(exclude_id)
        with self.db._connection() as conn:
            return self._decode(conn.execute(query + ' ORDER BY position LIMIT 1', args).fetchone())

    def enqueue_telegram(self, url, video_id, batch_id):
        from validators import canonical_tiktok_url
        canonical, actual_id = canonical_tiktok_url(url)
        if actual_id != str(video_id):
            raise ValueError('Ссылка и идентификатор видео не совпадают')
        with self.db._connection(write=True) as conn:
            previous = conn.execute('SELECT status FROM batch_telegram_queue WHERE url=?', (canonical,)).fetchone()
            if previous:
                return False
            outbox = conn.execute('SELECT status FROM telegram_outbox WHERE url=?', (canonical,)).fetchone()
            status = 'PENDING'
            if outbox and outbox['status'] == 'SENT':
                status = 'SENT'
            elif outbox and outbox['status'] not in ('FAILED',):
                status = 'UNKNOWN'
            now = _now()
            conn.execute('''INSERT INTO batch_telegram_queue(url,video_id,batch_id,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?)''', (canonical, str(video_id), batch_id, status, now, now))
            return status == 'PENDING'

    def telegram_queue(self, batch_id=None):
        query = 'SELECT * FROM batch_telegram_queue'
        args = ()
        if batch_id is not None:
            query += ' WHERE batch_id=?'
            args = (batch_id,)
        with self.db._connection() as conn:
            return [self._decode(row) for row in conn.execute(query + ' ORDER BY created_at,url', args)]

    def update_telegram(self, url, status, error=''):
        if status not in self.QUEUE_STATES or not isinstance(error, str):
            raise ValueError('Неверное состояние доставки')
        with self.db._connection(write=True) as conn:
            row = conn.execute('SELECT status FROM batch_telegram_queue WHERE url=?', (url,)).fetchone()
            if row is None:
                raise KeyError('Ссылка не поставлена в очередь отправки')
            if row['status'] == 'SENT' and status != 'SENT':
                raise ValueError('Подтверждённую доставку нельзя сбросить')
            if row['status'] == 'UNKNOWN' and status not in ('UNKNOWN', 'SENT'):
                raise ValueError('Сначала проверьте неизвестный результат доставки вручную')
            if row['status'] == 'IN_FLIGHT' and status == 'PENDING':
                raise ValueError('Отправка могла состояться; повторная постановка заблокирована')
            conn.execute('UPDATE batch_telegram_queue SET status=?,error=?,updated_at=? WHERE url=?',
                         (status, error, _now(), url))
        return next(row for row in self.telegram_queue() if row['url'] == url)
