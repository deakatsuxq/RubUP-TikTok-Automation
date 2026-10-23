"""User-supplied comment bank. This module never creates or sends comments."""
from __future__ import annotations

import re
import unicodedata

from automation_store import AutomationStore, _now


_BRAND = re.compile(r'(?<!\w)rubup(?!\w)', re.IGNORECASE)


def _normalize(text):
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().split())


class CommentBank:
    def __init__(self, store):
        self.store = store if isinstance(store, AutomationStore) else AutomationStore(store)
        self.db = self.store.db

    def import_text(self, text):
        if not isinstance(text, str):
            raise ValueError('Вставьте комментарии, каждый с новой строки')
        if len(text) > 2_000_000:
            raise ValueError('Текст банка слишком большой')
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) > 500:
            raise ValueError('За один раз можно добавить не более 500 комментариев')
        result = {'added': 0, 'duplicates': 0, 'rejected': 0, 'errors': []}
        with self.db._connection(write=True) as conn:
            for index, raw in enumerate(lines, 1):
                value = ' '.join(unicodedata.normalize('NFKC', raw).split())
                error = ''
                if any(ord(ch) < 32 and ch != '\t' or ord(ch) == 127 for ch in raw):
                    error = 'удалите управляющие символы'
                elif len(value) > 1000:
                    error = 'комментарий длиннее 1000 символов'
                elif not _BRAND.search(value):
                    error = 'добавьте обязательное упоминание RubUP'
                if error:
                    result['rejected'] += 1
                    result['errors'].append(f'Строка {index}: {error}')
                    continue
                inserted = conn.execute('''INSERT INTO v4_comments(text,normalized,created_at)
                    VALUES(?,?,?) ON CONFLICT(normalized) DO NOTHING''', (value, _normalize(value), _now())).rowcount
                result['added' if inserted else 'duplicates'] += 1
            if result['added']:
                self.store._event(conn, 'BANK_IMPORTED', detail=str(result['added']))
        return result

    def list_comments(self):
        with self.db._connection() as conn:
            rows = conn.execute('''SELECT c.id,c.text,c.used,c.use_count,
                EXISTS(SELECT 1 FROM v4_tasks t WHERE t.comment_id=c.id AND t.status<>'DONE') AS assigned
                FROM v4_comments c ORDER BY c.id''').fetchall()
            return [{**dict(row), 'used': bool(row['used']), 'assigned': bool(row['assigned'])} for row in rows]

    def stats(self):
        with self.db._connection() as conn:
            row = conn.execute('''SELECT COUNT(*) AS total,COALESCE(SUM(c.used),0) AS used,
                COALESCE(SUM(CASE WHEN c.used=0 AND NOT EXISTS(
                    SELECT 1 FROM v4_tasks t WHERE t.comment_id=c.id AND t.status<>'DONE') THEN 1 ELSE 0 END),0) AS available
                FROM v4_comments c''').fetchone()
            return dict(row)

    def reset_usage(self):
        """Release completed history only; queued texts remain safely reserved."""
        with self.db._connection(write=True) as conn:
            reset = conn.execute('''UPDATE v4_comments SET used=0 WHERE used=1 AND NOT EXISTS(
                SELECT 1 FROM v4_tasks t WHERE t.comment_id=v4_comments.id AND t.status<>'DONE')''').rowcount
            protected = conn.execute('''SELECT COUNT(*) FROM v4_comments c WHERE EXISTS(
                SELECT 1 FROM v4_tasks t WHERE t.comment_id=c.id AND t.status<>'DONE')''').fetchone()[0]
            available = conn.execute('SELECT COUNT(*) FROM v4_comments WHERE used=0').fetchone()[0]
            self.store._event(conn, 'USAGE_RESET_BY_USER', detail=f'reset={reset}; protected={protected}')
        return {'reset': reset, 'protected': protected, 'available': available}
