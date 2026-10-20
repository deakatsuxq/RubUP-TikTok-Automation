from types import SimpleNamespace

import pytest

from cost_tracker import CostTracker, calculate_cost


class ExampleDatabase:
    def __init__(self, total_cost=0.012):
        self.total_cost = total_cost

    def stats(self):
        return {"videos": 3, "published": 0, "estimated_reward": 4, "confirmed_reward": 0}

    def usage_summary(self):
        return {
            "requests": 3,
            "successful_generations": 2,
            "retries": 1,
            "input_tokens": 1500,
            "output_tokens": 200,
            "total_cost": self.total_cost,
            "known_cost": 0.012,
            "unknown_cost_requests": int(self.total_cost is None),
        }


def config(**overrides):
    values = dict(input_price_per_million=0.4, output_price_per_million=1.6, third_party_cost=0)
    values.update(overrides)
    return SimpleNamespace(**values)


def test_one_response_cost_uses_both_token_types():
    assert calculate_cost(1_000_000, 1_000_000, 0.4, 1.6) == 2
    assert calculate_cost(250, 40, 0.4, 1.6) == pytest.approx(0.000164)


@pytest.mark.parametrize("rates", [(None, None), (0.4, None), (None, 1.6)])
def test_unknown_tariff_remains_unknown(rates):
    assert calculate_cost(250, 40, *rates) is None


@pytest.mark.parametrize("tokens", [-1, 1.5, True])
def test_invalid_token_counts_rejected(tokens):
    with pytest.raises(ValueError):
        calculate_cost(tokens, 40, 0.4, 1.6)


@pytest.mark.parametrize("price", [-0.1, float("nan"), float("inf"), True, "bad"])
def test_invalid_prices_rejected(price):
    with pytest.raises(ValueError):
        calculate_cost(250, 40, price, 1.6)


def test_ten_thousand_estimate_does_not_make_generations():
    tracker = CostTracker(ExampleDatabase(), config(third_party_cost=2))
    result = tracker.estimate()
    assert result["count"] == 10_000
    assert result["input_tokens"] == 2_500_000
    assert result["output_tokens"] == 400_000
    assert result["openai_cost"] == pytest.approx(1.64)
    assert result["third_party_cost"] == 2
    assert result["total_cost"] == pytest.approx(3.64)
    assert result["preliminary"] is True
    assert result["pricing_source"] == "manual_configuration"


def test_estimate_without_configured_tariff_is_not_free():
    result = CostTracker(ExampleDatabase(), config(output_price_per_million=None)).estimate()
    assert result["total_cost"] is None
    assert result["openai_cost"] is None
    assert result["pricing_known"] is False


def test_usage_summary_includes_failed_response_costs():
    result = CostTracker(ExampleDatabase(), config()).summary()
    assert result["requests"] == 3
    assert result["retries"] == 1
    assert result["average_comment_cost"] == pytest.approx(0.006)
    assert result["confirmed_reward"] == 0
    assert result["estimate_10000"]["count"] == 10_000


def test_partial_known_cost_does_not_hide_unknown_responses():
    result = CostTracker(ExampleDatabase(total_cost=None), config()).summary()
    assert result["known_cost"] == 0.012
    assert result["total_cost"] is None
    assert result["average_comment_cost"] is None


def test_empty_volume_and_zero_tariff_are_valid():
    result = CostTracker(ExampleDatabase(), config(input_price_per_million=0, output_price_per_million=0)).estimate(0)
    assert result["total_cost"] == 0
    assert result["pricing_known"] is True


def test_negative_generation_count_rejected():
    with pytest.raises(ValueError):
        CostTracker(ExampleDatabase(), config()).estimate(-1)
