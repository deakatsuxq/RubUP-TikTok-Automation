"""Durable per-batch limits based only on the user's configured USD tariffs.

The input reservation uses UTF-8 byte length (an upper bound for byte-pair text
tokens), plus 1,024 tokens for message framing, and the configured output cap.
It is intentionally conservative and is not a claim about the provider's bill.
Actual usage reconciles reservations; an ambiguous result keeps its reservation.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_CEILING
import uuid


_PROCESS = uuid.uuid4().hex
_MILLION = Decimal(1_000_000)
_QUANTUM = Decimal('0.00000001')


class BudgetError(ValueError):
    def __init__(self, message, reason='invalid'):
        super().__init__(message)
        self.reason = reason


def _money(value, *, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, bool):
        raise BudgetError('Лимит и тарифы должны быть конечными неотрицательными числами')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise BudgetError('Не удалось прочитать лимит или тарифы') from None
    if not result.is_finite() or result < 0:
        raise BudgetError('Лимит и тарифы должны быть конечными неотрицательными числами')
    return result


class BudgetGuard:
    def __init__(self, db, batch_id, config, limit_usd=None):
        self.db, self.batch_id, self.config = db, str(batch_id), config
        if not self.batch_id:
            raise BudgetError('Партия не найдена')
        suggested = limit_usd if limit_usd is not None else getattr(config, 'batch_budget_usd', None)
        proposed = _money(suggested, optional=True)
        # BatchStore performs the backed-up additive migration first. These
        # extra tables never alter old task/history schemas or their versions.
        with db._connection(write=True) as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS batch_budget_limits (
                batch_id TEXT PRIMARY KEY, limit_usd TEXT
            )''')
            conn.execute('''CREATE TABLE IF NOT EXISTS budget_reservations (
                token TEXT PRIMARY KEY, batch_id TEXT NOT NULL,
                amount TEXT, actual_cost TEXT,
                state TEXT NOT NULL CHECK(state IN
                    ('RESERVED','SETTLED','REJECTED','UNKNOWN','UNPRICED')),
                owner TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )''')
            conn.execute('CREATE INDEX IF NOT EXISTS budget_batch ON budget_reservations(batch_id)')
            conn.execute('INSERT INTO batch_budget_limits(batch_id,limit_usd) VALUES (?,?) '
                         'ON CONFLICT(batch_id) DO NOTHING',
                         (self.batch_id, None if proposed is None else str(proposed)))
            saved = conn.execute('SELECT limit_usd FROM batch_budget_limits WHERE batch_id=?',
                                 (self.batch_id,)).fetchone()[0]
            self._initial_reserved = {row[0] for row in conn.execute(
                "SELECT token FROM budget_reservations WHERE batch_id=? AND state='RESERVED'", (self.batch_id,))}
        self.limit_usd = _money(saved, optional=True)

    def estimate_request(self, context, instructions, max_output_tokens):
        if type(max_output_tokens) is not int or max_output_tokens < 0:
            raise BudgetError('Некорректный предел ответа модели')
        incoming = _money(getattr(self.config, 'input_price_per_million', None), optional=True)
        outgoing = _money(getattr(self.config, 'output_price_per_million', None), optional=True)
        if incoming is None or outgoing is None:
            return None
        inputs = len((str(instructions) + '\n' + str(context)).encode('utf-8')) + 1024
        estimate = (Decimal(inputs) * incoming + Decimal(max_output_tokens) * outgoing) / _MILLION
        return estimate.quantize(_QUANTUM, rounding=ROUND_CEILING)

    def _totals(self, conn):
        rows = conn.execute('SELECT amount,actual_cost,state,owner FROM budget_reservations '
                            'WHERE batch_id=?', (self.batch_id,)).fetchall()
        spent = sum((_money(row['actual_cost']) for row in rows
                     if row['state'] in ('SETTLED', 'REJECTED')), Decimal(0))
        reserved = sum((_money(row['amount']) for row in rows
                        if row['state'] in ('RESERVED', 'UNKNOWN') and row['amount'] is not None), Decimal(0))
        unknown = sum(row['state'] == 'UNKNOWN' or
                      (row['state'] == 'RESERVED' and row['owner'] != _PROCESS) for row in rows)
        return spent, reserved, unknown, sum(row['state'] == 'UNPRICED' for row in rows), len(rows)

    def before_request(self, context, instructions, max_output_tokens):
        estimate = self.estimate_request(context, instructions, max_output_tokens)
        if self.limit_usd is not None and estimate is None:
            raise BudgetError('Для ограничения расходов укажите тарифы входных и выходных токенов модели',
                              'unknown_price')
        with self.db._connection(write=True) as conn:
            spent, reserved, unknown, _, _ = self._totals(conn)
            unresolved = conn.execute("SELECT token FROM budget_reservations WHERE batch_id=? AND state='RESERVED'",
                                      (self.batch_id,)).fetchall()
            if any(row[0] in self._initial_reserved for row in unresolved):
                # A recreated guard is used for a resumed preparation. It
                # cannot prove whether the preceding reserved call was sent.
                # Do not alter rows in a summary-only guard constructor.
                raise BudgetError('Предыдущий запрос не завершён подтверждённым результатом. Проверьте расходы у провайдера',
                                  'unknown_cost')
            if unknown:
                raise BudgetError('Стоимость предыдущего запроса неизвестна. Новые платные запросы этой партии остановлены',
                                  'unknown_cost')
            if self.limit_usd is not None and spent + reserved + estimate > self.limit_usd:
                raise BudgetError('Достигнут лимит расходов партии: новый запрос не выполнен', 'limit')
            token = uuid.uuid4().hex
            conn.execute('INSERT INTO budget_reservations(token,batch_id,amount,state,owner) '
                         "VALUES (?,?,?,'RESERVED',?)",
                         (token, self.batch_id, None if estimate is None else str(estimate), _PROCESS))
        return token

    def settle(self, reservation, cost, known_rejected=False, usage_known=None):
        actual = Decimal(0) if known_rejected else _money(cost, optional=True)
        with self.db._connection(write=True) as conn:
            row = conn.execute('SELECT * FROM budget_reservations WHERE token=? AND batch_id=?',
                               (reservation, self.batch_id)).fetchone()
            if row is None:
                raise BudgetError('Резерв запроса не найден')
            if row['state'] != 'RESERVED':
                # Settlement is idempotent, but uncertainty is never silently
                # cleared by an automatic second call or a restarted process.
                return self.summary()
            if known_rejected:
                state = 'REJECTED'
            elif actual is not None:
                state = 'SETTLED'
            elif usage_known is True and self.limit_usd is None:
                # A valid measured response without manual tariffs is not an
                # ambiguous delivery. Unlimited mode may continue, honestly
                # retaining unknown total price rather than claiming it is free.
                state = 'UNPRICED'
            else:
                state = 'UNKNOWN'
            conn.execute('UPDATE budget_reservations SET state=?,actual_cost=? WHERE token=?',
                         (state, None if actual is None else str(actual), reservation))
        return self.summary()

    def summary(self):
        with self.db._connection() as conn:
            spent, reserved, unknown, unpriced, requests = self._totals(conn)
        remaining = None if self.limit_usd is None else max(Decimal(0), self.limit_usd - spent - reserved)
        return {
            'limit_usd': None if self.limit_usd is None else float(self.limit_usd),
            'spent_usd': float(spent), 'reserved_usd': float(reserved),
            'unknown_requests': unknown, 'unpriced_requests': unpriced, 'requests': requests,
            'remaining_usd': None if remaining is None else float(remaining),
            'total_cost_known': not unknown and not unpriced,
            'over_limit': self.limit_usd is not None and spent + reserved > self.limit_usd,
        }
