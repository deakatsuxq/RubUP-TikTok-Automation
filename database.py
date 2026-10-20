"""Thread-safe, durable SQLite storage for videos, tasks and independent outcomes."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
import re
from pathlib import Path
import sqlite3
import threading
import unicodedata
import uuid


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(re.sub(r"[^\w\s]", "", text).split())


class Database:
    """Every operation has its own connection; writes commit atomically.

    A shared in-memory keeper is provided for tests. Production databases use WAL,
    a busy timeout and foreign keys. Empty comments are excluded from uniqueness.
    """

    TASK_FIELDS = frozenset({
        "comment", "image_slot", "processing_status", "publication_status",
        "telegram_status", "report_status", "reward_status", "expected_reward",
        "confirmed_reward", "attempts", "error", "checklist", "is_test",
    })
    VIDEO_FIELDS = frozenset({
        "url", "author", "description", "hashtags", "published_at", "query",
        "category", "is_test",
    })

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._keeper = None
        self._lock = threading.RLock()
        self._uri = self.path == ":memory:"
        if self._uri:
            self.path = f"file:rubup-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._keeper = sqlite3.connect(self.path, uri=True, check_same_thread=False)
        else:
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS videos (
                    video_id TEXT PRIMARY KEY,
                    url TEXT NOT NULL UNIQUE,
                    author TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    hashtags TEXT NOT NULL DEFAULT '[]',
                    published_at TEXT,
                    discovered_at TEXT NOT NULL,
                    query TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    is_test INTEGER NOT NULL DEFAULT 0 CHECK(is_test IN (0,1))
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    video_id TEXT NOT NULL REFERENCES videos(video_id),
                    comment TEXT NOT NULL DEFAULT '',
                    comment_normalized TEXT,
                    image_slot INTEGER CHECK(image_slot BETWEEN 1 AND 4),
                    processing_status TEXT NOT NULL DEFAULT 'NEW'
                        CHECK(processing_status IN ('NEW','ANALYZED','DRAFT_READY',
                            'PENDING_APPROVAL','PUBLISHED','FAILED','SKIPPED')),
                    publication_status TEXT NOT NULL DEFAULT 'UNCONFIRMED'
                        CHECK(publication_status IN ('UNCONFIRMED','PUBLISHED')),
                    telegram_status TEXT NOT NULL DEFAULT 'NOT_SENT'
                        CHECK(telegram_status IN ('NOT_SENT','RESERVED','IN_FLIGHT','SENDING',
                            'SENT','FAILED','UNKNOWN','SKIPPED')),
                    report_status TEXT NOT NULL DEFAULT 'NONE'
                        CHECK(report_status IN ('NONE','REPORT_SUBMITTED','REPORT_ACCEPTED')),
                    reward_status TEXT NOT NULL DEFAULT 'UNCONFIRMED'
                        CHECK(reward_status IN ('UNCONFIRMED','REWARD_CONFIRMED')),
                    expected_reward REAL NOT NULL DEFAULT 0 CHECK(expected_reward >= 0),
                    confirmed_reward REAL NOT NULL DEFAULT 0 CHECK(confirmed_reward >= 0),
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                    error TEXT NOT NULL DEFAULT '',
                    checklist TEXT NOT NULL DEFAULT '{}',
                    is_test INTEGER NOT NULL DEFAULT 0 CHECK(is_test IN (0,1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(account_id,video_id),
                    CHECK(report_status = 'NONE' OR publication_status='PUBLISHED'),
                    CHECK(reward_status='UNCONFIRMED' OR report_status='REPORT_ACCEPTED')
                );
                CREATE UNIQUE INDEX IF NOT EXISTS tasks_comment_unique
                    ON tasks(comment_normalized) WHERE comment_normalized IS NOT NULL;
                CREATE INDEX IF NOT EXISTS tasks_queue ON tasks(processing_status,account_id);
                CREATE INDEX IF NOT EXISTS videos_date ON videos(published_at);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER REFERENCES tasks(id),
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER REFERENCES tasks(id),
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL CHECK(input_tokens >= 0),
                    output_tokens INTEGER NOT NULL CHECK(output_tokens >= 0),
                    cost REAL CHECK(cost >= 0),
                    success INTEGER NOT NULL CHECK(success IN (0,1)),
                    attempt INTEGER NOT NULL CHECK(attempt >= 1),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS telegram_outbox (
                    url TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                PRAGMA user_version=1;
            """)

    @contextmanager
    def _connection(self, write=False):
        # Serialize this instance's writers and in-memory operations. Independent
        # processes are protected by SQLite's transaction lock and unique indexes.
        with self._lock:
            conn = sqlite3.connect(self.path, uri=self._uri, timeout=15)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=15000")
            try:
                if write:
                    conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        result = dict(row)
        for key in ("hashtags", "checklist"):
            if key in result:
                result[key] = json.loads(result[key])
        if "is_test" in result:
            result["is_test"] = bool(result["is_test"])
        result.pop("comment_normalized", None)
        return result

    def add_video(self, video) -> bool:
        data = asdict(video) if is_dataclass(video) else dict(video)
        if not str(data.get("video_id", "")).strip() or not data.get("url"):
            raise ValueError("Видео должно содержать ID и URL")
        values = {key: data.get(key, "") for key in self.VIDEO_FIELDS}
        values["published_at"] = data.get("published_at") or None
        values["hashtags"] = json.dumps(data.get("hashtags", []), ensure_ascii=False)
        values["is_test"] = int(bool(data.get("is_test", False)))
        values["video_id"] = str(data["video_id"])
        values["discovered_at"] = _now()
        keys = list(values)
        with self._connection(write=True) as conn:
            cur = conn.execute(
                f"INSERT INTO videos ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)}) "
                "ON CONFLICT DO NOTHING", [values[key] for key in keys])
            return cur.rowcount == 1

    def update_video(self, video_id, **fields):
        unknown = set(fields) - self.VIDEO_FIELDS
        if unknown:
            raise ValueError(f"Неизвестные поля видео: {', '.join(sorted(unknown))}")
        if not fields:
            return
        if "hashtags" in fields:
            fields["hashtags"] = json.dumps(fields["hashtags"], ensure_ascii=False)
        if "is_test" in fields:
            fields["is_test"] = int(bool(fields["is_test"]))
        with self._connection(write=True) as conn:
            result = conn.execute(
                f"UPDATE videos SET {','.join(key+'=?' for key in fields)} WHERE video_id=?",
                [*fields.values(), str(video_id)])
            if not result.rowcount:
                raise KeyError(f"Видео не найдено: {video_id}")

    def get_video(self, video_id):
        with self._connection() as conn:
            return self._decode(conn.execute("SELECT * FROM videos WHERE video_id=?", (str(video_id),)).fetchone())

    def list_videos(self):
        with self._connection() as conn:
            return [self._decode(row) for row in conn.execute("SELECT * FROM videos ORDER BY discovered_at,video_id")]

    def ensure_task(self, account_id, video_id):
        if not str(account_id).strip():
            raise ValueError("Укажите ID TikTok-аккаунта")
        with self._connection(write=True) as conn:
            video = conn.execute("SELECT is_test FROM videos WHERE video_id=?", (str(video_id),)).fetchone()
            if video is None:
                raise KeyError(f"Видео не найдено: {video_id}")
            now = _now()
            conn.execute("""INSERT INTO tasks(account_id,video_id,is_test,created_at,updated_at)
                VALUES (?,?,?,?,?) ON CONFLICT(account_id,video_id) DO NOTHING""",
                (str(account_id), str(video_id), video["is_test"], now, now))
            row = conn.execute("SELECT id FROM tasks WHERE account_id=? AND video_id=?", (str(account_id),str(video_id))).fetchone()
            task_id = row["id"]
        return self.get_task(task_id)

    _JOIN = """SELECT v.*,t.* FROM tasks t JOIN videos v ON v.video_id=t.video_id"""

    def get_task(self, task_id):
        with self._connection() as conn:
            row = conn.execute(self._JOIN + " WHERE t.id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"Задача не найдена: {task_id}")
            return self._decode(row)

    def list_tasks(self, account_id=None):
        query = self._JOIN
        args = ()
        if account_id is not None:
            query += " WHERE t.account_id=?"
            args = (str(account_id),)
        with self._connection() as conn:
            return [self._decode(row) for row in conn.execute(query + " ORDER BY t.id", args)]

    def update_task(self, task_id, **fields):
        unknown = set(fields) - self.TASK_FIELDS
        if unknown:
            raise ValueError(f"Неизвестные поля задачи: {', '.join(sorted(unknown))}")
        if not fields:
            return
        with self._connection(write=True) as conn:
            before = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if before is None:
                raise KeyError(f"Задача не найдена: {task_id}")
            if before["publication_status"] == "PUBLISHED":
                for key in ("comment", "image_slot"):
                    if key in fields and fields[key] != before[key]:
                        raise ValueError("Подтверждённую публикацию нельзя заменить новым черновиком")
                if fields.get("publication_status", "PUBLISHED") != "PUBLISHED":
                    raise ValueError("Подтверждённая публикация не может быть сброшена")
            if "comment" in fields:
                fields["comment"] = str(fields["comment"]).strip()
                fields["comment_normalized"] = _normalize(fields["comment"]) or None
            if "checklist" in fields:
                if not isinstance(fields["checklist"], dict):
                    raise ValueError("Чек-лист должен быть словарём")
                fields["checklist"] = json.dumps(fields["checklist"], ensure_ascii=False, sort_keys=True)
            if "is_test" in fields:
                fields["is_test"] = int(bool(fields["is_test"]))
            fields["updated_at"] = _now()
            conn.execute(f"UPDATE tasks SET {','.join(key+'=?' for key in fields)} WHERE id=?", [*fields.values(),task_id])
            changed = {k: v for k, v in fields.items() if k not in {"updated_at","comment_normalized"} and v != before[k]}
            if changed:
                conn.execute("INSERT INTO events(task_id,kind,detail,created_at) VALUES (?,?,?,?)", (
                    task_id, "TASK_UPDATED", json.dumps(changed,ensure_ascii=False), _now()))

    def recent_comments(self, limit=30, exclude_task_id=None):
        if int(limit) < 0:
            raise ValueError("Лимит не может быть отрицательным")
        query = "SELECT comment FROM tasks WHERE comment<>''"
        args = []
        if exclude_task_id is not None:
            query += " AND id<>?"
            args.append(exclude_task_id)
        query += " ORDER BY updated_at DESC,id DESC LIMIT ?"
        args.append(int(limit))
        with self._connection() as conn:
            return [row[0] for row in conn.execute(query,args)]

    def comment_exists(self, text, exclude_task_id=None):
        normalized = _normalize(text)
        if not normalized:
            return False
        query = "SELECT 1 FROM tasks WHERE comment_normalized=?"
        args = [normalized]
        if exclude_task_id is not None:
            query += " AND id<>?"
            args.append(exclude_task_id)
        with self._connection() as conn:
            return conn.execute(query,args).fetchone() is not None

    def add_event(self, task_id, kind, detail=""):
        if not isinstance(detail,str):
            detail = json.dumps(detail, ensure_ascii=False)
        with self._connection(write=True) as conn:
            conn.execute("INSERT INTO events(task_id,kind,detail,created_at) VALUES (?,?,?,?)",(task_id,str(kind),detail,_now()))

    def events(self):
        with self._connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM events ORDER BY id")]

    def record_usage(self, task_id, model, input_tokens, output_tokens, cost, success, attempt):
        if any(isinstance(x,bool) or not isinstance(x,int) or x < 0 for x in (input_tokens,output_tokens)):
            raise ValueError("Число токенов должно быть целым и неотрицательным")
        if not isinstance(attempt,int) or attempt < 1:
            raise ValueError("Номер попытки должен быть положительным")
        if cost is not None:
            import math
            if not math.isfinite(float(cost)) or float(cost) < 0:
                raise ValueError("Стоимость должна быть конечной и неотрицательной")
        with self._connection(write=True) as conn:
            conn.execute("""INSERT INTO usage(task_id,model,input_tokens,output_tokens,cost,success,attempt,created_at)
                VALUES (?,?,?,?,?,?,?,?)""", (task_id,str(model),input_tokens,output_tokens,cost,int(bool(success)),attempt,_now()))

    def usage_summary(self):
        with self._connection() as conn:
            row = conn.execute("""SELECT COUNT(*) requests,
                COALESCE(SUM(success),0) successful_generations,
                COALESCE(SUM(CASE WHEN attempt>1 THEN 1 ELSE 0 END),0) retries,
                COALESCE(SUM(input_tokens),0) input_tokens,
                COALESCE(SUM(output_tokens),0) output_tokens,
                COALESCE(SUM(cost),0) known_cost,
                COALESCE(SUM(CASE WHEN cost IS NULL THEN 1 ELSE 0 END),0) unknown_cost_requests
                FROM usage""").fetchone()
            result = dict(row)
            result["total_cost"] = None if result["unknown_cost_requests"] else result["known_cost"]
            return result

    def reserve_telegram(self, url):
        """Reserve before sending. Unknown/crashed reservations never auto-retry."""
        with self._connection(write=True) as conn:
            existing = conn.execute("SELECT status FROM telegram_outbox WHERE url=?",(url,)).fetchone()
            if existing and existing["status"] != "FAILED":
                return False
            if existing:
                conn.execute("UPDATE telegram_outbox SET status='RESERVED',error='',attempts=attempts+1,updated_at=? WHERE url=?",(_now(),url))
            else:
                conn.execute("INSERT INTO telegram_outbox(url,status,updated_at) VALUES (?,'RESERVED',?)",(url,_now()))
            return True

    def finish_telegram(self, url, status, error=""):
        if status not in {"RESERVED","IN_FLIGHT","SENDING","SENT","FAILED","UNKNOWN"}:
            raise ValueError("Неизвестный статус отправки Telegram")
        with self._connection(write=True) as conn:
            row = conn.execute("SELECT status FROM telegram_outbox WHERE url=?",(url,)).fetchone()
            if row is None:
                raise KeyError("Ссылка не была зарезервирована перед отправкой")
            if row["status"] == "SENT" and status != "SENT":
                raise ValueError("Успешная отправка не может быть сброшена")
            if row["status"] == "UNKNOWN" and status not in {"UNKNOWN","SENT"}:
                raise ValueError("Неопределённый результат требует проверки пользователем")
            conn.execute("UPDATE telegram_outbox SET status=?,error=?,updated_at=? WHERE url=?",(status,str(error),_now(),url))
            conn.execute("""UPDATE tasks SET telegram_status=?,updated_at=?
                WHERE video_id IN (SELECT video_id FROM videos WHERE url=?)""",(status,_now(),url))

    def telegram_status(self, url):
        with self._connection() as conn:
            row = conn.execute("SELECT status FROM telegram_outbox WHERE url=?",(url,)).fetchone()
            return row[0] if row else None

    def stats(self):
        with self._connection() as conn:
            result = dict(conn.execute("""SELECT COUNT(*) tasks,
                COALESCE(SUM(CASE WHEN comment<>'' THEN 1 ELSE 0 END),0) drafts,
                COALESCE(SUM(CASE WHEN publication_status='PUBLISHED' THEN 1 ELSE 0 END),0) published,
                COALESCE(SUM(CASE WHEN processing_status='FAILED' THEN 1 ELSE 0 END),0) errors,
                COALESCE(SUM(CASE WHEN processing_status='SKIPPED' THEN 1 ELSE 0 END),0) skipped,
                COALESCE(SUM(expected_reward),0) estimated_reward,
                COALESCE(SUM(CASE WHEN reward_status='REWARD_CONFIRMED' THEN confirmed_reward ELSE 0 END),0) confirmed_reward
                FROM tasks""").fetchone())
            result["videos"] = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
            result["telegram_sent"] = conn.execute("SELECT COUNT(*) FROM telegram_outbox WHERE status='SENT'").fetchone()[0]
            result["processed"] = conn.execute("SELECT COUNT(*) FROM tasks WHERE processing_status<>'NEW'").fetchone()[0]
            result["events_errors"] = conn.execute("SELECT COUNT(*) FROM events WHERE kind LIKE '%ERROR%' OR kind LIKE '%FAILED%'").fetchone()[0]
        result.update(self.usage_summary())
        return result

    def close(self):
        if self._keeper is not None:
            self._keeper.close()
            self._keeper = None
