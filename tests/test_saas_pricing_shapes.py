"""Tests for the SaaS pricing shape (#241, #246).

``transactional`` is the one shape left: a percentage of each transaction's
value plus fixed fees. Other SaaS prices are vendor price rows, and
``tests/test_vendors_are_data.py`` covers them. This file covers the handler,
the registry and engine integration: a shaped metric prices through its
handler instead of the catalog or the embedded ``pricingRates``.
"""

import pytest

from infra_cost_model.saas import SaaSPricingRegistry, transactional
from infra_cost_model.engine import CostEngine
from infra_cost_model.pricing.catalog import SECONDS_PER_MONTH

STRIPE = {"percentage_rate": 0.029, "fixed_per_transaction": 0.30, "volume": 50.0}


# ── Built-in shape handler ───────────────────────────────────────────────


class TestTransactional:
    """transactional: the preserved percentage/per-call shape."""

    def test_percentage_plus_fixed(self):
        """2.9% + $0.30 on each of 100 transactions of $10 (#288)."""
        cost = transactional(100, {
            "percentage_rate": 0.029,
            "fixed_per_transaction": 0.30,
            "volume": 10.0,
        })
        assert cost == pytest.approx(100 * (10 * 0.029 + 0.30))  # $29 + $30 = $59

    def test_percentage_is_charged_on_every_transaction(self):
        """``volume`` is the value of one transaction, as in the engine's
        ``percentage`` model (#281): 1,000 payments of $50 cost $1,750."""
        cost = transactional(1000, {
            "percentage_rate": 0.029,
            "fixed_per_transaction": 0.30,
            "volume": 50.0,
        })
        assert cost == pytest.approx(1750.0)

    def test_per_call(self):
        """Twilio-style: $0.0075 per call, 1000 calls."""
        cost = transactional(1000, {"per_call": 0.0075})
        assert cost == pytest.approx(7.5)

    def test_zero_transactions(self):
        assert transactional(0, {"per_call": 0.01}) == 0.0


# ── Registry ─────────────────────────────────────────────────────────────


class TestSaaSPricingRegistry:
    """The shape registry."""

    def test_only_transactional_is_registered(self):
        assert SaaSPricingRegistry.known_shapes() == {"transactional"}

    def test_get_returns_handler(self):
        """get() returns the callable for a known shape."""
        handler = SaaSPricingRegistry.get("transactional")
        assert handler is not None
        assert handler(5, {"per_call": 2.0}) == 10.0

    def test_get_unknown_shape_returns_none(self):
        """get() returns None for an unregistered shape."""
        assert SaaSPricingRegistry.get("nonexistent_shape") is None

    def test_compute_unknown_shape_raises(self):
        """compute() raises ValueError for an unknown shape — hard error."""
        with pytest.raises(ValueError, match="Unknown pricing shape 'nonexistent'"):
            SaaSPricingRegistry.compute("nonexistent", 100, {})

    def test_register_custom_shape(self):
        """A caller that builds a model in code can register a shape."""
        def my_shape(quantity, params):
            return quantity * float(params.get("rate", 1.0)) + 10.0

        SaaSPricingRegistry.register("my_custom_shape", my_shape)
        try:
            assert SaaSPricingRegistry.compute("my_custom_shape", 5, {"rate": 2.0}) == 20.0
        finally:
            SaaSPricingRegistry._handlers.pop("my_custom_shape", None)

    def test_reset_clears_handlers(self):
        """reset() clears all handlers (for testing)."""
        SaaSPricingRegistry.reset()
        try:
            assert SaaSPricingRegistry.known_shapes() == set()
        finally:
            SaaSPricingRegistry.register("transactional", transactional)


# ── Engine integration ───────────────────────────────────────────────────


ENTRY = {
    "nodeType": "routing",
    "resourceAddress": "entry",
    "provider": "test",
    "service": "Test",
    "region": "global",
    "usageMetrics": {"requests": {"unit": "requests", "value": 1}},
    "pricingRates": {"requests": 0.0},
}


def _saas_node(metrics, **extra):
    return {
        "nodeType": "external",
        "resourceAddress": "saas_node",
        "provider": "external",
        "service": "Payments",
        "region": "global",
        "pricingModel": "flat",
        "usageMetrics": metrics,
        **extra,
    }


class TestEngineShapeIntegration:
    """A shaped metric in a DAG prices through the shape handler."""

    def _make_engine(self, nodes, edges=None):
        model = {
            "workflow": {"name": "test", "entry": "entry", "frequency": {"unit": "perMonth", "value": 1000}},
            "nodes": {"entry": ENTRY, **nodes},
            "edges": edges or [],
        }
        return CostEngine(model, catalog=None, time_basis="monthly")

    def test_fixed_transactional_in_engine(self):
        """A fixed count of 3 transactions of $50: 3 × ($1.45 + $0.30)."""
        nodes = {"saas_node": _saas_node(
            {"charges": {"unit": "transactions", "value": 3, "shape": "transactional", **STRIPE}},
            flatOverride=True,
        )}
        costs = self._make_engine(nodes).compute()
        assert costs["saas_node"] == pytest.approx(3 * 1.75)

    def test_usage_driven_transactional(self):
        """A shaped metric that's not fixed scales with the invocation count."""
        nodes = {"saas_node": _saas_node(
            {"calls": {"unit": "calls", "value": 2, "shape": "transactional", "per_call": 0.01}},
        )}
        edges = [{"from": "entry", "to": "saas_node", "rate": 1}]
        costs = self._make_engine(nodes, edges).compute()
        # 1,000 requests × 2 calls × $0.01 = $20
        assert costs["saas_node"] == pytest.approx(20.0)

    def test_unknown_shape_raises_error(self):
        """An unregistered shape raises ValueError — no silent fallback."""
        nodes = {"saas_node": _saas_node(
            {"Hosts": {"unit": "Hosts", "value": 4, "shape": "nonexistent_shape"}},
            flatOverride=True, pricingRates={"Hosts": 46.0},
        )}
        with pytest.raises(ValueError, match="Unknown pricing shape 'nonexistent_shape'"):
            self._make_engine(nodes).compute()

    @pytest.mark.parametrize("shape", ["free_tier", "per_unit_flat", "flat_subscription"])
    def test_removed_shape_raises_with_the_replacement(self, shape):
        """A model the schema would reject still fails in the engine (#246)."""
        nodes = {"saas_node": _saas_node(
            {"Hosts": {"unit": "Hosts", "value": 4, "shape": shape, "rate": 23.0}},
            flatOverride=True,
        )}
        with pytest.raises(ValueError, match="vendor price rows"):
            self._make_engine(nodes).compute()

    def test_no_shape_uses_existing_path(self):
        """A metric without a shape uses the existing catalog/pricingRates path."""
        nodes = {"saas_node": _saas_node(
            {"Hosts": {"unit": "Hosts", "value": 4}},
            flatOverride=True, pricingRates={"Hosts": 46.0},
        )}
        costs = self._make_engine(nodes).compute()
        # No shape → existing path → 4 × $46 = $184
        assert costs["saas_node"] == pytest.approx(184.0)


class TestShapesGetMonthlyQuantities:
    """A usage-driven shaped metric is priced on a month of usage (#295).

    The engine derives usage per second, while shape parameters describe a
    month. The engine passes each handler the monthly quantity and converts
    the monthly cost to the output time basis, the way catalog tiers work
    since #292.
    """

    # Output time basis → how many months it covers.
    BASES = {
        "monthly": 1.0,
        "yearly": 12.0,
        "perSecond": 1.0 / SECONDS_PER_MONTH,
    }

    @staticmethod
    def _model(metric, invocations, pricing_model="flat", fixed=False):
        metric = {"unit": "units", "value": 1, **metric}
        if fixed:
            metric["fixed"] = True
        return {
            "workflow": {
                "name": "test", "entry": "entry",
                "frequency": {"unit": "perMonth", "value": invocations},
            },
            "nodes": {
                "entry": ENTRY,
                "saas_node": _saas_node({"Metric": metric}, pricingModel=pricing_model),
            },
            # A fixed node takes no edge, which would only warn (DP#9).
            "edges": [] if fixed else [{"from": "entry", "to": "saas_node", "rate": 1}],
        }

    @pytest.mark.parametrize("basis", list(BASES))
    def test_transactional_at_time_basis(self, basis):
        """1,000 × ($50 × 0.029 + $0.30) = $1,750 a month (#288)."""
        model = self._model({"shape": "transactional", **STRIPE}, 1000)
        costs = CostEngine(model, catalog=None, time_basis=basis).compute()
        assert costs["saas_node"] == pytest.approx(1750.0 * self.BASES[basis])

    def test_tiered_pricing_model_gets_monthly_quantity(self):
        """The tiered pricing model dispatches shapes the same way."""
        model = self._model({"shape": "transactional", **STRIPE}, 1000,
                            pricing_model="tiered")
        costs = CostEngine(model, catalog=None, time_basis="monthly").compute()
        assert costs["saas_node"] == pytest.approx(1750.0)

    @pytest.mark.parametrize("basis", ["monthly", "yearly"])
    def test_fixed_metric_is_a_monthly_total(self, basis):
        """A fixed metric's value is already a monthly total: 2 × $1.75."""
        model = self._model({"shape": "transactional", **STRIPE, "value": 2},
                            1000, fixed=True)
        costs = CostEngine(model, catalog=None, time_basis=basis).compute()
        assert costs["saas_node"] == pytest.approx(3.5 * self.BASES[basis])

    def test_registered_shape_gets_monthly_quantity(self):
        """A shape registered in code also receives the monthly quantity."""
        seen = []

        def recording_shape(quantity, params):
            seen.append(quantity)
            return quantity * float(params["rate"])

        SaaSPricingRegistry.register("recording_shape", recording_shape)
        try:
            model = self._model({"shape": "recording_shape", "rate": 0.5}, 1000)
            costs = CostEngine(model, catalog=None, time_basis="monthly").compute()
        finally:
            SaaSPricingRegistry._handlers.pop("recording_shape", None)
        assert seen == [pytest.approx(1000.0)]
        assert costs["saas_node"] == pytest.approx(500.0)
