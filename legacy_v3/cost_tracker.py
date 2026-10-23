"""Honest, local estimates based on explicitly configured manual USD tariffs.

No tariff is fetched or invented here.  A missing input/output price makes the
corresponding complete estimate unknown, rather than silently treating it as free.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


MILLION = Decimal(1_000_000)


def _count(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name}: требуется целое число не меньше нуля")
    return value


def _price(value: Any, name: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name}: требуется конечная неотрицательная цена")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name}: некорректная цена") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{name}: требуется конечная неотрицательная цена")
    return result


def calculate_cost(
    input_tokens: int,
    output_tokens: int,
    input_price_per_million: float | None = None,
    output_price_per_million: float | None = None,
) -> float | None:
    """Calculate one response's USD cost, or None when pricing is unknown."""
    count_in = _count(input_tokens, "Входные токены")
    count_out = _count(output_tokens, "Выходные токены")
    rate_in = _price(input_price_per_million, "Тариф входных токенов")
    rate_out = _price(output_price_per_million, "Тариф выходных токенов")
    if rate_in is None or rate_out is None:
        return None
    return float((Decimal(count_in) * rate_in + Decimal(count_out) * rate_out) / MILLION)


class CostTracker:
    """Read usage without modifying it and provide bounded-volume forecasts."""

    def __init__(self, db: Any, config: Any) -> None:
        self.db = db
        self.config = config

    def estimate(
        self, count: int = 10_000, input_tokens: int = 250, output_tokens: int = 40
    ) -> dict[str, Any]:
        """Forecast without making API calls or creating any synthetic records.

        `count` counts API responses, not guaranteed successful comments. Rejected
        responses and retries can increase actual request count. The external
        source expense is a separately configured, fixed amount in USD.
        """
        _count(count, "Количество запросов")
        per_request = calculate_cost(
            input_tokens,
            output_tokens,
            getattr(self.config, "input_price_per_million", None),
            getattr(self.config, "output_price_per_million", None),
        )
        source_cost = _price(getattr(self.config, "third_party_cost", 0), "Расходы источника")
        source_total = float(source_cost) if source_cost is not None else None
        openai_total = (
            float(Decimal(str(per_request)) * count) if per_request is not None else None
        )
        combined = (
            float(Decimal(str(openai_total)) + Decimal(str(source_total)))
            if openai_total is not None and source_total is not None
            else None
        )
        return {
            "count": count,
            "input_tokens_per_request": input_tokens,
            "output_tokens_per_request": output_tokens,
            "input_tokens": input_tokens * count,
            "output_tokens": output_tokens * count,
            "cost_per_request": per_request,
            "openai_cost": openai_total,
            "third_party_cost": source_total,
            "total_cost": combined,
            "currency": "USD",
            "preliminary": True,
            "pricing_known": per_request is not None,
            "pricing_source": "manual_configuration",
            "note": (
                "Предварительный расчёт по ручным тарифам; повторы и отклонённые ответы оплачиваются отдельно."
                if per_request is not None
                else "Укажите актуальные тарифы модели: стоимость пока неизвестна."
            ),
        }

    def summary(self) -> dict[str, Any]:
        """Return measured usage; partial known costs never masquerade as total."""
        summary = dict(self.db.stats())
        summary.update(self.db.usage_summary())
        successful = summary.get("successful_generations", 0)
        total = summary.get("total_cost")
        summary["average_comment_cost"] = (
            float(Decimal(str(total)) / successful)
            if total is not None and successful > 0
            else None
        )
        summary["third_party_cost"] = float(
            _price(getattr(self.config, "third_party_cost", 0), "Расходы источника") or 0
        )
        summary["currency"] = "USD"
        summary["preliminary"] = True
        summary["pricing_source"] = "manual_configuration"
        summary["estimate_10000"] = self.estimate()
        return summary
