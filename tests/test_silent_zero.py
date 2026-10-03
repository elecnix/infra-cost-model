"""A model that prices as $0.00 without saying so must refuse, not report a total.

`compute()` used to read a usage metric's quantity as `metric.get("value", 0)`
and a percentage node's rates off the node instead of the metric, so a model
could report $0.000000 with no unpriced metric and no warning (#432). These
tests pin the refusals that close those paths, and pin the zeros that are
legitimate so a later change cannot forbid them.
"""

import pytest

from infra_cost_model.engine.engine import (
    CostEngine,
    SilentZeroError,
    silent_zero_errors,
)
from infra_cost_model.pricing.catalog import SECONDS_PER_MONTH

WF = {"entry": "w", "frequency": {"value": 1, "unit": "perMonth"}}


def model(nodes, **overrides):
    body = {"workflow": WF, "nodes": nodes, "edges": []}
    body.update(overrides)
    return body


class TestMetricWithoutValueRefuses:
    def test_error_names_the_node_and_the_metric(self):
        errors = silent_zero_errors(model({
            "w": {"nodeType": "compute",
                  "usageMetrics": {"m": {"unit": "requests"}}},
        }))
        assert len(errors) == 1
        assert "'w'" in errors[0] and "'m'" in errors[0]

    def test_compute_raises(self):
        """A metric that says how much of what, and never says how much."""
        with pytest.raises(SilentZeroError) as excinfo:
            CostEngine(model({
                "w": {"nodeType": "compute",
                      "usageMetrics": {"m": {"unit": "requests"}}},
            })).compute()
        assert "'m'" in str(excinfo.value)

    def test_fixed_metric_without_value(self):
        """A fixed metric's value is its monthly total; absent means zero."""
        with pytest.raises(SilentZeroError):
            CostEngine(model({
                "w": {"nodeType": "compute",
                      "usageMetrics": {"m": {"unit": "requests", "fixed": True}}},
            })).compute()

    def test_one_bad_metric_in_a_healthy_model_still_refuses(self):
        with pytest.raises(SilentZeroError) as excinfo:
            CostEngine(model({
                "w": {"nodeType": "compute",
                      "usageMetrics": {"requests": {"unit": "requests", "value": 1}}},
                "d": {"nodeType": "compute",
                      "usageMetrics": {"gb": {"unit": "GB"}}},
            }, edges=[{"from": "w", "to": "d", "rate": 1}])).compute()
        assert "'d'" in str(excinfo.value)

    def test_multi_workflow_refuses_too(self):
        body = {
            "workflows": [
                {"name": "a", "entry": "w",
                 "frequency": {"value": 1, "unit": "perMonth"}},
            ],
            "nodes": {"w": {"nodeType": "compute",
                            "usageMetrics": {"m": {"unit": "requests"}}}},
        }
        with pytest.raises(SilentZeroError):
            CostEngine(body).compute()

    def test_silent_zero_error_is_a_value_error(self):
        """Callers that already catch ValueError keep working."""
        assert issubclass(SilentZeroError, ValueError)


class TestLegitimateZeroStillPrices:
    def test_explicit_zero_value_is_not_a_refusal(self):
        costs = CostEngine(model({
            "w": {"nodeType": "compute",
                  "usageMetrics": {"freeTier": {"unit": "requests", "value": 0}}},
        })).compute()
        assert costs == {"w": 0.0}

    def test_zero_frequency_is_an_instruction_not_a_miss(self):
        costs = CostEngine(model(
            {"w": {"nodeType": "compute",
                   "usageMetrics": {"m": {"unit": "requests", "value": 1000}}}},
            workflow={"entry": "w", "frequency": {"value": 0, "unit": "perMonth"}},
        )).compute()
        assert costs == {"w": 0.0}

    def test_zero_edge_rate_is_an_instruction_not_a_miss(self):
        costs = CostEngine(model(
            {
                "w": {"nodeType": "compute",
                      "usageMetrics": {"m": {"unit": "requests", "value": 1000}}},
                "d": {"nodeType": "compute",
                      "usageMetrics": {"m": {"unit": "requests", "value": 1000}}},
            },
            edges=[{"from": "w", "to": "d", "rate": 0}],
        )).compute()
        assert costs == {"w": 0.0, "d": 0.0}


class TestExemptMetrics:
    def test_shape_metric_prices_from_its_own_quantity(self):
        """A shape takes its rate inline; the count still comes off `value`."""
        costs = CostEngine(model({
            "w": {"nodeType": "external",
                  "usageMetrics": {"transactions": {
                      "unit": "transactions", "value": 1, "shape": "transactional",
                      "percentage_rate": 0.029, "volume": 50.0,
                      "fixed_per_transaction": 0.30}}},
        })).compute()
        assert costs["w"] == pytest.approx(1.75 / SECONDS_PER_MONTH)

    def test_a_shape_metric_without_a_count_still_refuses(self):
        with pytest.raises(SilentZeroError):
            CostEngine(model({
                "w": {"nodeType": "external",
                      "usageMetrics": {"transactions": {
                          "unit": "transactions", "shape": "transactional",
                          "percentage_rate": 0.029, "volume": 50.0}}},
            })).compute()

    def test_token_based_node_prices_token_flow_not_a_quantity(self):
        costs = CostEngine(model({
            "w": {"nodeType": "compute", "pricingModel": "token_based",
                  "provider": "bedrock", "service": "AnthropicClaude",
                  "region": "us-east-1",
                  "pricingRates": {"inputTokens": 3e-6},
                  "usageMetrics": {"inputTokens": {"unit": "tokens"}}},
        })).compute()
        assert costs["w"] == 0.0  # no upstream tokens: legitimately zero

    def test_a_node_with_no_metrics_is_not_a_refusal(self):
        assert silent_zero_errors(model({"w": {"nodeType": "compute"}})) == []


class TestPercentageReadsTheMetric:
    """`pricingModel: percentage` reads the metric the schema documents."""

    def test_inline_rates_price_the_transaction(self):
        costs = CostEngine(model({
            "w": {
                "nodeType": "external",
                "pricingModel": "percentage",
                "usageMetrics": {"transactionVolume": {
                    "unit": "USD", "value": 50,
                    "percentage_rate": 0.029, "fixed_per_transaction": 0.30}},
            },
        })).compute()
        # 1 transaction a month of $50: 50 × 0.029 + 0.30
        assert costs["w"] == pytest.approx(1.75 / SECONDS_PER_MONTH)

    def test_node_pricing_rates_remain_the_escape_hatch(self):
        """Principle 9: a model may state its rates on the node instead."""
        costs = CostEngine(model({
            "w": {
                "nodeType": "external",
                "pricingModel": "percentage",
                "pricingRates": {"percentageRate": 0.029,
                                 "fixedPerTransaction": 0.30},
                "usageMetrics": {"transactionVolume": {"unit": "USD", "value": 50}},
            },
        })).compute()
        assert costs["w"] == pytest.approx(1.75 / SECONDS_PER_MONTH)

    def test_the_metric_rate_wins_over_the_node_rate(self):
        costs = CostEngine(model({
            "w": {
                "nodeType": "external",
                "pricingModel": "percentage",
                "pricingRates": {"percentageRate": 0.029,
                                 "fixedPerTransaction": 0.30},
                "usageMetrics": {"transactionVolume": {
                    "unit": "USD", "value": 50, "percentage_rate": 0.039}},
            },
        })).compute()
        assert costs["w"] == pytest.approx((50 * 0.039 + 0.30) / SECONDS_PER_MONTH)

    def test_per_call_is_charged_once_per_transaction(self):
        costs = CostEngine(model({
            "w": {
                "nodeType": "external",
                "pricingModel": "percentage",
                "usageMetrics": {"transactionVolume": {
                    "unit": "USD", "value": 50, "percentage_rate": 0.029,
                    "per_call": 0.10}},
            },
        })).compute()
        assert costs["w"] == pytest.approx((50 * 0.029 + 0.10) / SECONDS_PER_MONTH)

    def test_volume_wins_over_value(self):
        costs = CostEngine(model({
            "w": {
                "nodeType": "external",
                "pricingModel": "percentage",
                "usageMetrics": {"transactionVolume": {
                    "unit": "USD", "value": 1, "volume": 50,
                    "percentage_rate": 0.029}},
            },
        })).compute()
        assert costs["w"] == pytest.approx(50 * 0.029 / SECONDS_PER_MONTH)


class TestPercentageWithoutARateRefuses:
    def test_no_rate_anywhere(self):
        with pytest.raises(SilentZeroError) as excinfo:
            CostEngine(model({
                "w": {
                    "nodeType": "external",
                    "pricingModel": "percentage",
                    "usageMetrics": {"transactionVolume": {"unit": "USD", "value": 50}},
                },
            })).compute()
        assert "percentage" in str(excinfo.value)

    def test_a_rate_but_no_transaction_value(self):
        errors = silent_zero_errors(model({
            "w": {
                "nodeType": "external",
                "pricingModel": "percentage",
                "usageMetrics": {"transactionVolume": {
                    "unit": "USD", "percentage_rate": 0.029}},
            },
        }))
        assert len(errors) == 1
        assert "'w'" in errors[0]

    def test_an_empty_percentage_node_is_reported(self):
        assert silent_zero_errors(model({
            "w": {"nodeType": "external", "pricingModel": "percentage"},
        }))

    def test_a_percentage_rate_with_no_transaction_value_is_reported(self):
        """A variable rate with nothing to charge it on bills the model at 0."""
        errors = silent_zero_errors(model({
            "w": {
                "nodeType": "external",
                "pricingModel": "percentage",
                "pricingRates": {"percentageRate": 0.029},
                "usageMetrics": {"requests": {"unit": "requests", "value": 10}},
            },
        }))
        assert len(errors) == 1
        assert "'w'" in errors[0]


class TestModelWithNoNodes:
    def test_silent_zero_errors_tolerates_a_model_without_nodes(self):
        assert silent_zero_errors({}) == []
        assert silent_zero_errors({"nodes": "not a dict"}) == []
