"""Durable RubUP 4 queues without changing any earlier version's records.

Comment assignment is a single SQLite transaction. A bank entry remains spent
until the user explicitly resets usage; a reset never releases an unfinished
assignment and never permits the same text for both accounts on the same video.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import secrets
import sqlite3
import unicodedata
import uuid

from scheduler import ProcessLock
from validators import canonical_tiktok_url


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class AutomationStore:
    STATUSES = {'WAITING', 'READY', 'FILLED', 'PUBLISHED', 'DONE', 'FAILED'}
    _TASK_QUERY = '''SELECT t.*,v.url,p.slot,p.name AS profile_name,
        COALESCE(c.text,'') AS comment FROM v4_tasks t
        JOIN v4_videos v ON v.video_id=t.video_id
        JOIN v4_profiles p ON p.id=t.profile_id
        LEFT JOIN v4_comments c ON c.id=t.comment_id'''

    def __init__(self, db):
        self.db = db
        self.backup_path = None
        self._migrate()

    def _migrate(self):
        lock = None
        if not getattr(self.db, '_uri', False):
            lock = ProcessLock(str(self.db.path) + '.v4-migration.lock')
            if not lock.acquire():
                raise ValueError('Другое окно программы обновляет базу. Повторите позже.')
        try:
            with self.db._lock:
                with self.db._connection() as conn:
                    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='v4_schema'").fetchone()
                    version = conn.execute('SELECT version FROM v4_schema WHERE singleton=1').fetchone() if exists else None
                    if version:
                        if version['version'] != 4:
                            raise ValueError('Версия базы не поддерживается. Исходный файл сохранён.')
                        return
                if not getattr(self.db, '_uri', False):
                    self.backup_path = self._backup()
                statements = [
                    '''CREATE TABLE IF NOT EXISTS v4_schema (
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL)''',
                    '''CREATE TABLE IF NOT EXISTS v4_profiles (
                        id TEXT PRIMARY KEY,slot INTEGER NOT NULL UNIQUE CHECK(slot IN (1,2)),
                        name TEXT NOT NULL,name_key TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL)''',
                    '''CREATE TABLE IF NOT EXISTS v4_comments (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,text TEXT NOT NULL,
                        normalized TEXT NOT NULL UNIQUE,used INTEGER NOT NULL DEFAULT 0 CHECK(used IN (0,1)),
                        use_count INTEGER NOT NULL DEFAULT 0 CHECK(use_count>=0),created_at TEXT NOT NULL)''',
                    '''CREATE TABLE IF NOT EXISTS v4_videos (
                        video_id TEXT PRIMARY KEY,url TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL)''',
                    '''CREATE TABLE IF NOT EXISTS v4_tasks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,profile_id TEXT NOT NULL REFERENCES v4_profiles(id),
                        video_id TEXT NOT NULL REFERENCES v4_videos(video_id),
                        comment_id INTEGER REFERENCES v4_comments(id),
                        status TEXT NOT NULL DEFAULT 'WAITING'
                            CHECK(status IN ('WAITING','READY','FILLED','PUBLISHED','DONE','FAILED')),
                        published INTEGER NOT NULL DEFAULT 0 CHECK(published IN (0,1)),
                        liked INTEGER NOT NULL DEFAULT 0 CHECK(liked IN (0,1)),
                        filled_at TEXT,error TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                        UNIQUE(profile_id,video_id),CHECK(liked<=published),
                        CHECK(published=0 OR comment_id IS NOT NULL))''',
                    'CREATE INDEX IF NOT EXISTS v4_tasks_queue ON v4_tasks(profile_id,status,id)',
                    'CREATE INDEX IF NOT EXISTS v4_tasks_comment ON v4_tasks(comment_id,status)',
                    '''CREATE TABLE IF NOT EXISTS v4_settings (
                        key TEXT PRIMARY KEY,value TEXT NOT NULL)''',
                    '''CREATE TABLE IF NOT EXISTS v4_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,task_id INTEGER REFERENCES v4_tasks(id),
                        kind TEXT NOT NULL,detail TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL)''',
                ]
                with self.db._connection(write=True) as conn:
                    for statement in statements:
                        conn.execute(statement)
                    now = _now()
                    for slot in (1, 2):
                        name = f'Аккаунт {slot}'
                        conn.execute('''INSERT INTO v4_profiles(id,slot,name,name_key,created_at)
                            VALUES (?,?,?,?,?) ON CONFLICT(slot) DO NOTHING''',
                            (f'v4-account-{uuid.uuid4().hex}', slot, name, name.casefold(), now))
                    conn.execute('INSERT INTO v4_schema(singleton,version) VALUES(1,4) ON CONFLICT(singleton) DO NOTHING')
        finally:
            if lock:
                lock.release()

    def _backup(self):
        source = Path(self.db.path).resolve()
        folder = source.parent / 'backups'
        folder.mkdir(parents=True, exist_ok=True)
        folder.chmod(0o700)
        target = folder / f'{source.name}.pre-v4.{uuid.uuid4().hex}.sqlite3'
        try:
            with sqlite3.connect(source) as original, sqlite3.connect(target) as backup:
                original.backup(backup)
                if backup.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise sqlite3.DatabaseError('backup integrity')
            target.chmod(0o600)
        except Exception:
            target.unlink(missing_ok=True)
            raise ValueError('Не удалось сохранить резервную копию базы. Обновление не выполнено.') from None
        return target

    @staticmethod
    def _task(row):
        if row is None:
            raise ValueError('Задание не найдено')
        value = dict(row)
        value['published'] = bool(value['published'])
        value['liked'] = bool(value['liked'])
        return value

    @staticmethod
    def _event(conn, kind, task_id=None, detail=''):
        conn.execute('INSERT INTO v4_events(task_id,kind,detail,created_at) VALUES(?,?,?,?)',
                     (task_id, kind, detail, _now()))

    def profiles(self):
        with self.db._connection() as conn:
            return [dict(row) for row in conn.execute('SELECT id,slot,name FROM v4_profiles ORDER BY slot')]

    def save_profile(self, slot, name):
        if type(slot) is not int or slot not in (1, 2):
            raise ValueError('Выберите аккаунт 1 или 2')
        if (not isinstance(name, str) or not name.strip() or len(name.strip()) > 80
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in name)):
            raise ValueError('Имя аккаунта должно содержать от 1 до 80 символов в одной строке')
        name = unicodedata.normalize('NFKC', name.strip())
        name_key = ' '.join(name.casefold().split())
        with self.db._connection(write=True) as conn:
            if conn.execute('SELECT 1 FROM v4_profiles WHERE slot<>? AND name_key=?', (slot, name_key)).fetchone():
                raise ValueError('Укажите разные имена для двух аккаунтов')
            conn.execute('UPDATE v4_profiles SET name=?,name_key=? WHERE slot=?', (name, name_key, slot))
            return dict(conn.execute('SELECT id,slot,name FROM v4_profiles WHERE slot=?', (slot,)).fetchone())

    def add_videos(self, links):
        """Validate the entire input before committing either account's queue."""
        values = []
        for entry in links:
            if not isinstance(entry, (tuple, list)) or len(entry) != 2:
                raise ValueError('Для видео требуются прямая ссылка и ID')
            url, video_id = canonical_tiktok_url(entry[0])
            if video_id != str(entry[1]):
                raise ValueError('ID видео не соответствует ссылке')
            values.append((url, video_id))
        result = {'added': 0, 'duplicates': 0, 'tasks': 0, 'legacy_published': 0}
        with self.db._connection(write=True) as conn:
            profile_ids = [row[0] for row in conn.execute('SELECT id FROM v4_profiles ORDER BY slot')]
            now = _now()
            for url, video_id in values:
                existing = conn.execute('SELECT 1 FROM v4_videos WHERE video_id=?', (video_id,)).fetchone()
                if not existing and conn.execute("SELECT 1 FROM tasks WHERE video_id=? AND publication_status='PUBLISHED' LIMIT 1",
                                                 (video_id,)).fetchone():
                    # Earlier account IDs cannot prove which new browser slot
                    # they describe. Conservatively retain their publication
                    # evidence and avoid preparing another pair for this video.
                    result['duplicates'] += 1
                    result['legacy_published'] += 1
                    continue
                new = conn.execute('INSERT INTO v4_videos(video_id,url,created_at) VALUES(?,?,?) ON CONFLICT DO NOTHING',
                                   (video_id, url, now)).rowcount
                result['added' if new else 'duplicates'] += 1
                for profile_id in profile_ids:
                    result['tasks'] += conn.execute('''INSERT INTO v4_tasks(profile_id,video_id,created_at,updated_at)
                        VALUES(?,?,?,?) ON CONFLICT(profile_id,video_id) DO NOTHING''',
                        (profile_id, video_id, now, now)).rowcount
            if result['added']:
                self._event(conn, 'LINKS_IMPORTED', detail=str(result['added']))
        return result

    def tasks(self, profile_id=None, pending_only=False):
        where, params = [], []
        if profile_id is not None:
            where.append('t.profile_id=?')
            params.append(str(profile_id))
        if pending_only:
            where.append("t.status<>'DONE'")
        query = self._TASK_QUERY + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY t.id'
        with self.db._connection() as conn:
            return [self._task(row) for row in conn.execute(query, params)]

    def get_task(self, task_id):
        with self.db._connection() as conn:
            return self._task(conn.execute(self._TASK_QUERY + ' WHERE t.id=?', (task_id,)).fetchone())

    def assign_comments(self):
        """Reserve randomly chosen comments and update tasks in one transaction."""
        assigned = 0
        with self.db._connection(write=True) as conn:
            pending = conn.execute('SELECT id,video_id FROM v4_tasks WHERE comment_id IS NULL ORDER BY id').fetchall()
            available = [row['id'] for row in conn.execute('''SELECT c.id FROM v4_comments c WHERE c.used=0
                AND NOT EXISTS(SELECT 1 FROM v4_tasks t WHERE t.comment_id=c.id AND t.status<>'DONE')''')]
            secrets.SystemRandom().shuffle(available)
            forbidden = {
                task['id']: {row[0] for row in conn.execute(
                    'SELECT comment_id FROM v4_tasks WHERE video_id=? AND comment_id IS NOT NULL',
                    (task['video_id'],))}
                for task in pending
            }
            # A reset can leave several unfinished videos with different
            # previously used texts. Find augmenting paths among this *new*
            # allocation so a random early choice cannot create a false shortage.
            # Persisted assignments are never changed by this matching step.
            task_for_comment, comment_for_task = {}, {}
            for task in pending:
                if len(comment_for_task) == len(available):
                    break
                queue, seen_tasks, seen_comments = [task['id']], {task['id']}, set()
                parents, free_comment = {}, None
                for candidate_task in queue:
                    for comment_id in available:
                        if comment_id in seen_comments or comment_id in forbidden[candidate_task]:
                            continue
                        seen_comments.add(comment_id)
                        parents[comment_id] = candidate_task
                        occupant = task_for_comment.get(comment_id)
                        if occupant is None:
                            free_comment = comment_id
                            break
                        if occupant not in seen_tasks:
                            seen_tasks.add(occupant)
                            queue.append(occupant)
                    if free_comment is not None:
                        break
                while free_comment is not None:
                    candidate_task = parents[free_comment]
                    previous = comment_for_task.get(candidate_task)
                    comment_for_task[candidate_task] = free_comment
                    task_for_comment[free_comment] = candidate_task
                    free_comment = previous
            now = _now()
            for task_id, selected in comment_for_task.items():
                conn.execute('UPDATE v4_comments SET used=1,use_count=use_count+1 WHERE id=?', (selected,))
                conn.execute("UPDATE v4_tasks SET comment_id=?,status='READY',error='',updated_at=? WHERE id=?",
                             (selected, now, task_id))
                self._event(conn, 'COMMENT_ASSIGNED', task_id)
                assigned += 1
            missing = conn.execute('SELECT COUNT(*) FROM v4_tasks WHERE comment_id IS NULL').fetchone()[0]
            free = conn.execute('''SELECT COUNT(*) FROM v4_comments c WHERE c.used=0
                AND NOT EXISTS(SELECT 1 FROM v4_tasks t WHERE t.comment_id=c.id AND t.status<>'DONE')''').fetchone()[0]
        return {'assigned': assigned, 'missing': missing, 'available': free}

    def mark_filled(self, task_id):
        with self.db._connection(write=True) as conn:
            task = self._task(conn.execute(self._TASK_QUERY + ' WHERE t.id=?', (task_id,)).fetchone())
            if not task['comment_id']:
                raise ValueError('Сначала назначьте комментарий из банка')
            if not task['published']:
                now = _now()
                conn.execute("UPDATE v4_tasks SET status='FILLED',filled_at=?,error='',updated_at=? WHERE id=?", (now, now, task_id))
                self._event(conn, 'FIELD_FILLED', task_id)
        return self.get_task(task_id)

    def mark_error(self, task_id, error):
        error = ' '.join(str(error).split())[:1000]
        with self.db._connection(write=True) as conn:
            task = self._task(conn.execute(self._TASK_QUERY + ' WHERE t.id=?', (task_id,)).fetchone())
            if not task['published']:
                conn.execute("UPDATE v4_tasks SET status='FAILED',filled_at=NULL,error=?,updated_at=? WHERE id=?",
                             (error, _now(), task_id))
            else:
                conn.execute('UPDATE v4_tasks SET error=?,updated_at=? WHERE id=?', (error, _now(), task_id))
            self._event(conn, 'TASK_ERROR', task_id, error)
        return self.get_task(task_id)

    def confirm_publication(self, task_id):
        with self.db._connection(write=True) as conn:
            task = self._task(conn.execute(self._TASK_QUERY + ' WHERE t.id=?', (task_id,)).fetchone())
            if not task['published']:
                if task['status'] != 'FILLED' or not task['filled_at'] or not task['comment_id']:
                    raise ValueError('Сначала откройте ролик и подставьте комментарий через Playwright')
                conn.execute("UPDATE v4_tasks SET published=1,status='PUBLISHED',error='',updated_at=? WHERE id=?", (_now(), task_id))
                self._event(conn, 'PUBLICATION_CONFIRMED_BY_USER', task_id)
        return self.get_task(task_id)

    def confirm_like(self, task_id):
        with self.db._connection(write=True) as conn:
            task = self._task(conn.execute(self._TASK_QUERY + ' WHERE t.id=?', (task_id,)).fetchone())
            if not task['published']:
                raise ValueError('Сначала подтвердите публикацию своего комментария')
            if not task['liked']:
                conn.execute("UPDATE v4_tasks SET liked=1,status='DONE',error='',updated_at=? WHERE id=?", (_now(), task_id))
                self._event(conn, 'OWN_LIKE_CONFIRMED_BY_USER', task_id)
        return self.get_task(task_id)

    def eligible_videos(self):
        with self.db._connection() as conn:
            return [dict(row) for row in conn.execute('''SELECT v.url,v.video_id FROM v4_videos v
                JOIN v4_tasks t ON t.video_id=v.video_id GROUP BY v.video_id
                HAVING COUNT(*)=2 AND SUM(CASE WHEN t.published=1 AND t.liked=1 AND t.status='DONE' THEN 1 ELSE 0 END)=2
                ORDER BY MIN(t.id)''')]

    def stats(self):
        with self.db._connection() as conn:
            result = dict(conn.execute('''SELECT COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN status<>'DONE' THEN 1 ELSE 0 END),0) AS remaining,
                COALESCE(SUM(CASE WHEN status='DONE' THEN 1 ELSE 0 END),0) AS completed,
                COALESCE(SUM(published),0) AS published FROM v4_tasks''').fetchone())
            result['profiles'] = [dict(row) for row in conn.execute('''SELECT p.id,p.slot,p.name,
                COUNT(t.id) AS total,COALESCE(SUM(CASE WHEN t.status<>'DONE' THEN 1 ELSE 0 END),0) AS remaining,
                COALESCE(SUM(CASE WHEN t.status='DONE' THEN 1 ELSE 0 END),0) AS completed,
                COALESCE(SUM(t.published),0) AS published FROM v4_profiles p
                LEFT JOIN v4_tasks t ON t.profile_id=p.id GROUP BY p.id ORDER BY p.slot''')]
            result['used_comments'] = conn.execute('SELECT COUNT(*) FROM v4_comments WHERE used=1').fetchone()[0]
            result['available_comments'] = conn.execute('''SELECT COUNT(*) FROM v4_comments c WHERE c.used=0
                AND NOT EXISTS(SELECT 1 FROM v4_tasks t WHERE t.comment_id=c.id AND t.status<>'DONE')''').fetchone()[0]
            result['videos'] = conn.execute('SELECT COUNT(*) FROM v4_videos').fetchone()[0]
            result['telegram_sent'] = conn.execute("SELECT COUNT(*) FROM telegram_outbox o JOIN v4_videos v ON v.url=o.url WHERE o.status='SENT'").fetchone()[0]
            result['telegram_unknown'] = conn.execute("SELECT COUNT(*) FROM telegram_outbox o JOIN v4_videos v ON v.url=o.url WHERE o.status IN ('UNKNOWN','RESERVED','IN_FLIGHT','SENDING')").fetchone()[0]
            result['telegram_pending'] = conn.execute('''SELECT COUNT(*) FROM (
                SELECT v.video_id FROM v4_videos v JOIN v4_tasks t ON t.video_id=v.video_id
                LEFT JOIN telegram_outbox o ON o.url=v.url
                WHERE o.status IS NULL OR o.status='FAILED' GROUP BY v.video_id
                HAVING COUNT(*)=2 AND SUM(CASE WHEN t.status='DONE' AND t.published=1 AND t.liked=1 THEN 1 ELSE 0 END)=2)''').fetchone()[0]
        return result

    def get_setting(self, key, default=None):
        with self.db._connection() as conn:
            row = conn.execute('SELECT value FROM v4_settings WHERE key=?', (str(key),)).fetchone()
            return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        if (not isinstance(key, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}', key)
                or any(word in key for word in ('password', 'token', 'secret', 'cookie'))):
            raise ValueError('Недопустимое имя параметра; секреты сохраняются отдельно')
        try:
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError):
            raise ValueError('Параметр должен содержать обычное JSON-значение') from None
        if len(raw) > 16384:
            raise ValueError('Параметр слишком большой')
        with self.db._connection(write=True) as conn:
            conn.execute('INSERT INTO v4_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, raw))
