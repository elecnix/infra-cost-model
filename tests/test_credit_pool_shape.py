"""Tests for the credit_pool SaaS pricing shape (GitHub Copilot).

The credit_pool shape prices usage-based AI plans: subscription + overage
above an included credit allowance at a fixed per-credit rate. It is the
shape-level counterpart of infra_cost_model/pricing/github_copilot.py.
"""

import pytest

from infra_cost_model.saas import SaaSPricingRegistry, credit_pool
from infra_cost_model.engine import CostEngine


class TestCreditPool:
    """credit_pool: subscription + overage on a monthly credit allowance."""

    def test_within_allowance(self):
        """60k credits used, 100k included: just the subscription."""
        cost = credit_pool(60_000, {"subscription": 475.0, "includedCredits": 100_000})
        assert cost == pytest.approx(475.0)

    def test_exactly_at_allowance(self):
        assert credit_pool(100_000, {"subscription": 475.0, "includedCredits": 100_000}) == pytest.approx(475.0)

    def test_overage_billed_at_credit_value(self):
        """60k used, 47.5k included: $475 + 12.5k × $0.01 = $600."""
        cost = credit_pool(60_000, {
            "subscription": 475.0, "includedCredits": 47_500, "creditValue": 0.01,
        })
        assert cost == pytest.approx(600.0)

    def test_default_credit_value_is_github_rate(self):
        """creditValue defaults to $0.01 (GitHub's fixed AI-credit rate)."""
        cost = credit_pool(50_000, {"subscription": 10.0, "includedCredits": 1_500})
        assert cost == pytest.approx(10.0 + 48_500 * 0.01)

    def test_zero_usage_charges_subscription(self):
        """The subscription is charged even with no usage."""
        assert credit_pool(0, {"subscription": 19.0, "includedCredits": 1_900}) == pytest.approx(19.0)

    def test_missing_params_default_to_zero(self):
        """No subscription/allowance: everything is overage at the default rate."""
        assert credit_pool(100, {}) == pytest.approx(1.0)

    def test_registered_as_builtin(self):
        assert "credit_pool" in SaaSPricingRegistry.known_shapes()


class TestCreditPoolEngineIntegration:
    """A credit_pool metric in a DAG prices through the shape handler."""

    def _make_engine(self, credits_used):
        model = {
            "workflow": {
                "name": "copilot",
                "entry": "github_copilot.business",
                "frequency": {"unit": "perMonth", "value": 1},
                "parameters": {"creditsUsed": credits_used},
            },
            "nodes": {
                "github_copilot.business": {
                    "nodeType": "external",
                    "resourceAddress": "github_copilot.business",
                    "provider": "github",
                    "service": "Copilot",
                    "region": "global",
                    "pricingModel": "flat",
                    "flatOverride": True,
                    "usageMetrics": {
                        "credits": {
                            "unit": "credits",
                            "value": "creditsUsed",
                            "shape": "credit_pool",
                            "subscription": 475.0,
                            "includedCredits": 47_500,
                            "creditValue": 0.01,
                        },
                    },
                },
            },
            "edges": [],
        }
        return CostEngine(model, catalog=None, time_basis="monthly")

    def test_within_pool(self):
        engine = self._make_engine(40_000)
        costs = engine.compute()
        assert costs["github_copilot.business"] == pytest.approx(475.0)

    def test_overage(self):
        engine = self._make_engine(60_000)
        costs = engine.compute()
        assert costs["github_copilot.business"] == pytest.approx(600.0)

    def test_what_if_sweeps_credit_consumption(self):
        """Varying the symbolic creditsUsed parameter changes the cost."""
        from infra_cost_model.engine import SensitivityAnalyzer
        analyzer = SensitivityAnalyzer(self._make_engine(40_000).cost_model, catalog=None, time_basis="monthly")
        assert analyzer.what_if("creditsUsed", 60_000) == pytest.approx(600.0)
        assert analyzer.what_if("creditsUsed", 100_000) == pytest.approx(475.0 + 52_500 * 0.01)
