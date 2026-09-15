"""Tests for SaaS pricing-shape handlers (#241).

Covers the built-in shapes (flat_subscription, per_unit_flat, free_tier,
transactional), the registry, entry-point discovery, and engine integration —
a shaped metric in a cost model DAG prices through the shape handler instead of
the catalog / embedded pricingRates path.
"""

import pytest

from infra_cost_model.saas import (
    SaaSPricingRegistry,
    flat_subscription,
    free_tier,
    per_unit_flat,
    transactional,
    credit_pool,
)
from infra_cost_model.saas.pricing_shapes import discover_entry_point_handlers
from infra_cost_model.engine import CostEngine


# ── Built-in shape handlers ──────────────────────────────────────────────


class TestFlatSubscription:
    """flat_subscription: a fixed monthly fee, charged when enabled."""

    def test_zero_quantity_is_free(self):
        """A metric at 0 means the feature is off — no charge."""
        assert flat_subscription(0, {"rate": 99.0}) == 0.0

    def test_enabled_charges_once(self):
        """Flipping from 0 to 1 charges the rate once."""
        assert flat_subscription(1, {"rate": 99.0}) == 99.0

    def test_multi_instance_charges_per_unit(self):
        """3 custom domains → 3 × rate."""
        assert flat_subscription(3, {"rate": 99.0}) == 297.0

    def test_missing_rate_defaults_to_zero(self):
        """No rate param → $0 (defensive)."""
        assert flat_subscription(1, {}) == 0.0

    def test_fractional_quantity_charges_once(self):
        """A fractional quantity (unusual) charges once, not 0."""
        assert flat_subscription(0.5, {"rate": 50.0}) == 50.0


class TestPerUnitFlat:
    """per_unit_flat: $X × count."""

    def test_basic(self):
        """5 SSO connections at $125 each."""
        assert per_unit_flat(5, {"rate": 125.0}) == 625.0

    def test_zero_count(self):
        """0 connections → $0."""
        assert per_unit_flat(0, {"rate": 125.0}) == 0.0

    def test_missing_rate(self):
        assert per_unit_flat(10, {}) == 0.0


class TestFreeTier:
    """free_tier: first N units free, then overage."""

    def test_under_free_allowance(self):
        """900k MAU under 1M free → $0."""
        assert free_tier(900_000, {"free": 1_000_000, "overage": 0.0}) == 0.0

    def test_exactly_at_allowance(self):
        """Exactly 1M MAU → $0 (boundary)."""
        assert free_tier(1_000_000, {"free": 1_000_000, "overage": 0.0}) == 0.0

    def test_above_allowance_with_overage(self):
        """1.5M MAU, 1M free, $0.01 overage → 500k × $0.01 = $5000."""
        assert free_tier(1_500_000, {"free": 1_000_000, "overage": 0.01}) == 5000.0

    def test_zero_overage_above_allowance(self):
        """Above the free tier but overage rate is 0 → still $0."""
        assert free_tier(2_000_000, {"free": 1_000_000, "overage": 0.0}) == 0.0

    def test_no_free_allowance(self):
        """free=0 means every unit is billable."""
        assert free_tier(100, {"free": 0, "overage": 0.05}) == 5.0

    def test_stepped_tiers(self):
        """Tiers: first 50k overage at $0.01, above 50k at $0.005.

        75k billable → 50k × $0.01 + 25k × $0.005 = $500 + $125 = $625.
        """
        params = {
            "free": 1_000_000,
            "overage": 0.0,
            "tiers": [
                {"up_to": 50_000, "rate": 0.01},
                {"up_to": float("inf"), "rate": 0.005},
            ],
        }
        assert free_tier(1_075_000, params) == pytest.approx(625.0)

    def test_stepped_tiers_partial(self):
        """Only 30k billable, first tier up to 50k → 30k × $0.01 = $300."""
        params = {
            "free": 1_000_000,
            "tiers": [
                {"up_to": 50_000, "rate": 0.01},
                {"up_to": float("inf"), "rate": 0.005},
            ],
        }
        assert free_tier(1_030_000, params) == pytest.approx(300.0)


class TestTransactional:
    """transactional: the preserved percentage/per-call shape."""

    def test_percentage_plus_fixed(self):
        """2.9% + $0.30 per transaction, 100 transactions, $1000 volume."""
        cost = transactional(100, {
            "percentage_rate": 0.029,
            "fixed_per_transaction": 0.30,
            "volume": 1000.0,
        })
        assert cost == pytest.approx(1000 * 0.029 + 100 * 0.30)  # $29 + $30 = $59

    def test_per_call(self):
        """Twilio-style: $0.0075 per call, 1000 calls."""
        cost = transactional(1000, {"per_call": 0.0075})
        assert cost == pytest.approx(7.5)

    def test_zero_transactions(self):
        assert transactional(0, {"per_call": 0.01}) == 0.0


# ── Registry ─────────────────────────────────────────────────────────────


class TestSaaSPricingRegistry:
    """The pluggable shape registry."""

    def test_builtin_shapes_registered(self):
        """All five built-in shapes are registered at module load."""
        shapes = SaaSPricingRegistry.known_shapes()
        assert "flat_subscription" in shapes
        assert "per_unit_flat" in shapes
        assert "free_tier" in shapes
        assert "transactional" in shapes
        assert "credit_pool" in shapes

    def test_get_returns_handler(self):
        """get() returns the callable for a known shape."""
        handler = SaaSPricingRegistry.get("per_unit_flat")
        assert handler is not None
        assert callable(handler)
        assert handler(5, {"rate": 10.0}) == 50.0

    def test_get_unknown_shape_returns_none(self):
        """get() returns None for an unregistered shape."""
        assert SaaSPricingRegistry.get("nonexistent_shape") is None

    def test_compute_unknown_shape_returns_none(self):
        """compute() returns None for an unknown shape — enables fallback."""
        assert SaaSPricingRegistry.compute("nonexistent", 100, {}) is None

    def test_register_custom_shape(self):
        """A third-party shape can be registered and computed."""
        def my_shape(quantity, params):
            return quantity * float(params.get("rate", 1.0)) + 10.0

        SaaSPricingRegistry.register("my_custom_shape", my_shape)
        try:
            assert SaaSPricingRegistry.get("my_custom_shape") is not None
            assert SaaSPricingRegistry.compute("my_custom_shape", 5, {"rate": 2.0}) == 20.0
        finally:
            SaaSPricingRegistry.reset()
            # Re-register built-ins after reset (reset clears everything).
            SaaSPricingRegistry.register("flat_subscription", flat_subscription)
            SaaSPricingRegistry.register("per_unit_flat", per_unit_flat)
            SaaSPricingRegistry.register("free_tier", free_tier)
            SaaSPricingRegistry.register("transactional", transactional)
            SaaSPricingRegistry.register("credit_pool", credit_pool)

    def test_reset_clears_handlers(self):
        """reset() clears all handlers (for testing)."""
        SaaSPricingRegistry.reset()
        assert len(SaaSPricingRegistry.known_shapes()) == 0
        # Restore for other tests.
        SaaSPricingRegistry.register("flat_subscription", flat_subscription)
        SaaSPricingRegistry.register("per_unit_flat", per_unit_flat)
        SaaSPricingRegistry.register("free_tier", free_tier)
        SaaSPricingRegistry.register("transactional", transactional)
        SaaSPricingRegistry.register("credit_pool", credit_pool)


# ── Engine integration ───────────────────────────────────────────────────


class TestEngineShapeIntegration:
    """A shaped metric in a DAG prices through the shape handler."""

    def _make_engine(self, nodes, edges=None):
        model = {
            "workflow": {"name": "test", "entry": "entry", "frequency": {"unit": "perMonth", "value": 1000}},
            "nodes": nodes,
            "edges": edges or [],
        }
        return CostEngine(model, catalog=None, time_basis="monthly")

    def test_per_unit_flat_in_engine(self):
        """A per_unit_flat metric prices correctly in the engine."""
        nodes = {
            "entry": {
                "nodeType": "routing",
                "resourceAddress": "entry",
                "provider": "test",
                "service": "Test",
                "region": "global",
                "usageMetrics": {"requests": {"unit": "requests", "value": 1}},
                "pricingRates": {"requests": 0.0},
            },
            "saas_node": {
                "nodeType": "compute",
                "resourceAddress": "saas_node",
                "provider": "workos",
                "service": "WorkOS",
                "region": "global",
                "pricingModel": "flat",
                "flatOverride": True,
                "usageMetrics": {
                    "SSO-Connection": {
                        "unit": "Conns",
                        "value": 3,
                        "shape": "per_unit_flat",
                        "rate": 125.0,
                    },
                },
            },
        }
        engine = self._make_engine(nodes)
        costs = engine.compute()
        # 3 connections × $125 = $375
        assert costs["saas_node"] == pytest.approx(375.0)

    def test_free_tier_in_engine(self):
        """A free_tier metric prices correctly in the engine."""
        nodes = {
            "entry": {
                "nodeType": "routing",
                "resourceAddress": "entry",
                "provider": "test",
                "service": "Test",
                "region": "global",
                "usageMetrics": {"requests": {"unit": "requests", "value": 1}},
                "pricingRates": {"requests": 0.0},
            },
            "saas_node": {
                "nodeType": "compute",
                "resourceAddress": "saas_node",
                "provider": "workos",
                "service": "WorkOS",
                "region": "global",
                "pricingModel": "flat",
                "flatOverride": True,
                "usageMetrics": {
                    "MAU": {
                        "unit": "Users",
                        "value": 1_500_000,
                        "shape": "free_tier",
                        "free": 1_000_000,
                        "overage": 0.01,
                    },
                },
            },
        }
        engine = self._make_engine(nodes)
        costs = engine.compute()
        # 500k overage × $0.01 = $5000
        assert costs["saas_node"] == pytest.approx(5000.0)

    def test_flat_subscription_in_engine(self):
        """A flat_subscription metric charges the rate when enabled."""
        nodes = {
            "entry": {
                "nodeType": "routing",
                "resourceAddress": "entry",
                "provider": "test",
                "service": "Test",
                "region": "global",
                "usageMetrics": {"requests": {"unit": "requests", "value": 1}},
                "pricingRates": {"requests": 0.0},
            },
            "saas_node": {
                "nodeType": "compute",
                "resourceAddress": "saas_node",
                "provider": "workos",
                "service": "WorkOS",
                "region": "global",
                "pricingModel": "flat",
                "flatOverride": True,
                "usageMetrics": {
                    "CustomDomain": {
                        "unit": "Months",
                        "value": 1,
                        "shape": "flat_subscription",
                        "rate": 99.0,
                    },
                },
            },
        }
        engine = self._make_engine(nodes)
        costs = engine.compute()
        assert costs["saas_node"] == pytest.approx(99.0)

    def test_mixed_shapes_in_one_node(self):
        """A node with multiple shaped metrics (the WorkOS pattern)."""
        nodes = {
            "entry": {
                "nodeType": "routing",
                "resourceAddress": "entry",
                "provider": "test",
                "service": "Test",
                "region": "global",
                "usageMetrics": {"requests": {"unit": "requests", "value": 1}},
                "pricingRates": {"requests": 0.0},
            },
            "workos": {
                "nodeType": "compute",
                "resourceAddress": "workos",
                "provider": "workos",
                "service": "WorkOS",
                "region": "global",
                "pricingModel": "flat",
                "flatOverride": True,
                "usageMetrics": {
                    "MAU": {
                        "unit": "Users", "value": 1_200_000,
                        "shape": "free_tier", "free": 1_000_000, "overage": 0.0,
                    },
                    "SSO": {
                        "unit": "Conns", "value": 2,
                        "shape": "per_unit_flat", "rate": 125.0,
                    },
                    "AuditLog": {
                        "unit": "Orgs", "value": 3,
                        "shape": "per_unit_flat", "rate": 5.0,
                    },
                    "Domain": {
                        "unit": "Months", "value": 1,
                        "shape": "flat_subscription", "rate": 99.0,
                    },
                },
            },
        }
        engine = self._make_engine(nodes)
        costs = engine.compute()
        # MAU: 200k overage × $0 = $0
        # SSO: 2 × $125 = $250
        # AuditLog: 3 × $5 = $15
        # Domain: 1 × $99 = $99
        # Total: $364
        assert costs["workos"] == pytest.approx(364.0)

    def test_unknown_shape_falls_back_to_pricing_rates(self):
        """An unregistered shape falls back to embedded pricingRates."""
        nodes = {
            "entry": {
                "nodeType": "routing",
                "resourceAddress": "entry",
                "provider": "test",
                "service": "Test",
                "region": "global",
                "usageMetrics": {"requests": {"unit": "requests", "value": 1}},
                "pricingRates": {"requests": 0.0},
            },
            "saas_node": {
                "nodeType": "compute",
                "resourceAddress": "saas_node",
                "provider": "datadog",
                "service": "Datadog",
                "region": "global",
                "pricingModel": "flat",
                "flatOverride": True,
                "usageMetrics": {
                    "Hosts": {
                        "unit": "Hosts", "value": 4,
                        "shape": "nonexistent_shape",  # not registered
                    },
                },
                "pricingRates": {
                    "Hosts": 46.0,  # falls back to this
                },
            },
        }
        engine = self._make_engine(nodes)
        costs = engine.compute()
        # Unknown shape → None → falls back to pricingRates → 4 × $46 = $184
        assert costs["saas_node"] == pytest.approx(184.0)

    def test_no_shape_uses_existing_path(self):
        """A metric without a shape uses the existing catalog/pricingRates path."""
        nodes = {
            "entry": {
                "nodeType": "routing",
                "resourceAddress": "entry",
                "provider": "test",
                "service": "Test",
                "region": "global",
                "usageMetrics": {"requests": {"unit": "requests", "value": 1}},
                "pricingRates": {"requests": 0.0},
            },
            "saas_node": {
                "nodeType": "compute",
                "resourceAddress": "saas_node",
                "provider": "datadog",
                "service": "Datadog",
                "region": "global",
                "pricingModel": "flat",
                "flatOverride": True,
                "usageMetrics": {
                    "Hosts": {"unit": "Hosts", "value": 4},  # no shape
                },
                "pricingRates": {
                    "Hosts": 46.0,
                },
            },
        }
        engine = self._make_engine(nodes)
        costs = engine.compute()
        # No shape → existing path → 4 × $46 = $184
        assert costs["saas_node"] == pytest.approx(184.0)

    def test_usage_driven_shaped_metric(self):
        """A shaped metric that's NOT fixed scales with invocation count."""
        nodes = {
            "entry": {
                "nodeType": "routing",
                "resourceAddress": "entry",
                "provider": "test",
                "service": "Test",
                "region": "global",
                "usageMetrics": {"requests": {"unit": "requests", "value": 1}},
                "pricingRates": {"requests": 0.0},
            },
            "saas_node": {
                "nodeType": "compute",
                "resourceAddress": "saas_node",
                "provider": "datadog",
                "service": "Datadog",
                "region": "global",
                "pricingModel": "flat",
                # NO flatOverride — metrics are usage-driven by default
                "usageMetrics": {
                    "LogIngestion": {
                        "unit": "GB",
                        "value": 0.00002,  # per-request
                        "shape": "per_unit_flat",
                        "rate": 0.10,
                    },
                },
            },
        }
        edges = [{"from": "entry", "to": "saas_node", "rate": 1}]
        engine = self._make_engine(nodes, edges)
        costs = engine.compute()
        # 1000 requests × 0.00002 GB/req = 0.02 GB
        # 0.02 GB × $0.10/GB = $0.002
        assert costs["saas_node"] == pytest.approx(0.002)


# ── Entry-point discovery ────────────────────────────────────────────────


class TestEntryPointDiscovery:
    """Entry-point plugin discovery (no real plugins installed in tests)."""

    def test_discover_does_not_crash_without_plugins(self):
        """discover_entry_point_handlers() is safe to call with no plugins."""
        # Should not raise even though no infra_cost_model.saas_handlers
        # entry-points are installed in the test environment.
        discover_entry_point_handlers()
        # Built-ins should still be present.
        assert "flat_subscription" in SaaSPricingRegistry.known_shapes()