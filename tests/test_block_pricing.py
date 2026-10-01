"""Block pricing: a price row can state a price per block of N units (#369).

WorkOS bills $2,500 for each 1,000,000 monthly active users above the free
million, and a partly used block costs the whole block.
"""

import sqlite3

import pytest

from infra_cost_model.engine.engine import CostEngine
from infra_cost_model.pricing import vendors
from infra_cost_model.pricing.cache import Price, PricingCache, TieredPrice, billed_blocks
from infra_cost_model.pricing.vendors import _parse_row


def tiers(block_size=1_000_000, price=2500.0, free=1_000_000):
    def row(**kw):
        return Price(vendor="v", service="s", region="global", product_family=None,
                     attributes={}, usage_metric="m", unit="users", **kw)
    return TieredPrice([
        row(price_usd=0.0, start_usage_amount=0, end_usage_amount=free),
        row(price_usd=price, start_usage_amount=free, block_size=block_size),
    ])


@pytest.fixture
def workos(seed_catalog):
    return seed_catalog


def mau_cost(catalog, quantity, **kw):
    return catalog.query("workos", "WorkOS", "global", "AuthKit-MAU", quantity, **kw).total_cost


class TestBoundary:
    def test_below_the_free_million_costs_nothing(self, workos):
        assert mau_cost(workos, 999_999) == 0.0

    def test_exactly_the_free_million_costs_nothing(self, workos):
        assert mau_cost(workos, 1_000_000) == 0.0

    def test_one_user_over_starts_a_whole_block(self, workos):
        assert mau_cost(workos, 1_000_001) == 2500.0

    def test_partial_block_rounds_up(self, workos):
        assert mau_cost(workos, 1_200_000) == 2500.0

    def test_exact_block_does_not_round_up_again(self, workos):
        assert mau_cost(workos, 2_000_000) == 2500.0

    def test_one_user_into_the_second_block(self, workos):
        assert mau_cost(workos, 2_000_001) == 5000.0

    def test_tiered_price_agrees(self):
        t = tiers()
        assert t.total_cost(1_000_000) == 0.0
        assert t.total_cost(1_200_000) == 2500.0
        assert t.total_cost(2_000_001) == 5000.0


def test_billed_blocks():
    assert billed_blocks(0, 10) == 0
    assert billed_blocks(-5, 10) == 0
    assert billed_blocks(10, 10) == 1
    assert billed_blocks(10.0000000001, 10) == 1
    assert billed_blocks(10.5, 10) == 2


class TestWorkosRows:
    def test_audit_log_retention_is_99_per_million_events(self, workos):
        cost = lambda q: workos.query("workos", "WorkOS", "global",
                                      "AuditLog-Retention", q).total_cost
        assert cost(1) == 99.0
        assert cost(1_000_000) == 99.0
        assert cost(1_000_001) == 198.0

    def test_radar_has_a_free_thousand_then_100_per_50k_checks(self, workos):
        cost = lambda q: workos.query("workos", "WorkOS", "global",
                                      "Radar-Check", q).total_cost
        assert cost(1_000) == 0.0
        assert cost(1_001) == 100.0
        assert cost(51_000) == 100.0
        assert cost(51_001) == 200.0


class TestTimeBases:
    def model(self, value):
        return {
            "version": "1.0",
            "workflow": {"name": "t", "entry": "n", "frequency": {"unit": "perMonth", "value": 1}},
            "nodes": {"n": {"nodeType": "external", "resourceAddress": "n",
                            "provider": "workos", "service": "WorkOS", "region": "global",
                            "usageMetrics": {"AuthKit-MAU": {"unit": "users", "value": value,
                                                             "fixed": True}}}},
        }

    @pytest.mark.parametrize("basis,factor", [("monthly", 1), ("yearly", 12)])
    def test_bases_agree(self, workos, basis, factor):
        costs = CostEngine(self.model(1_200_000), catalog=workos, time_basis=basis).compute()
        assert costs["n"] == pytest.approx(2500.0 * factor)

    def test_per_second_agrees(self, workos):
        per_second = workos.query("workos", "WorkOS", "global", "AuthKit-MAU",
                                  1_200_000 / 2629800.0, period_seconds=1.0).total_cost
        assert per_second * 2629800.0 == pytest.approx(2500.0)

    def test_per_second_exact_multiple_does_not_gain_a_block(self, workos):
        per_second = workos.query("workos", "WorkOS", "global", "AuthKit-MAU",
                                  2_000_000 / 2629800.0, period_seconds=1.0).total_cost
        assert per_second * 2629800.0 == pytest.approx(2500.0)


class TestPooling:
    def model(self, a, b):
        def node(v):
            return {"nodeType": "external", "resourceAddress": "x", "provider": "workos",
                    "service": "WorkOS", "region": "global",
                    "usageMetrics": {"AuthKit-MAU": {"unit": "users", "value": v, "fixed": True}}}
        return {
            "version": "1.0",
            "workflow": {"name": "t", "entry": "a", "frequency": {"unit": "perMonth", "value": 1}},
            "nodes": {"a": node(a), "b": node(b)},
            "edges": [],
        }

    def test_two_nodes_pool_one_block_count(self, workos):
        # 600,000 + 600,000 = 1,200,000: one block, split by quantity. Priced
        # apart, each node is inside the free million and the total is $0.
        costs = CostEngine(self.model(600_000, 600_000), catalog=workos,
                           time_basis="monthly").compute()
        assert costs["a"] == pytest.approx(1250.0)
        assert costs["b"] == pytest.approx(1250.0)

    def test_blocks_round_up_once_on_the_total(self, workos):
        # 1,500,000 + 1,500,000 = 3,000,000: two blocks. Rounding each share
        # up would give three.
        costs = CostEngine(self.model(1_500_000, 1_500_000), catalog=workos,
                           time_basis="monthly").compute()
        assert costs["a"] + costs["b"] == pytest.approx(5000.0)


class TestLoader:
    ROW = {"vendor": "v", "service": "s", "usage_metric": "m", "unit": "u", "price_usd": 1}

    def parse(self, **extra):
        return _parse_row({**self.ROW, **extra}, "src", 1, "now")

    def test_default_is_no_block(self):
        assert self.parse().block_size is None

    def test_block_size_is_read(self):
        assert self.parse(block_size=50000).block_size == 50000.0

    @pytest.mark.parametrize("bad", [0, -1, "10", True, float("inf")])
    def test_bad_block_size_is_refused(self, bad):
        with pytest.raises(ValueError, match="block_size"):
            self.parse(block_size=bad)


class TestCacheMigration:
    def test_old_database_gains_the_column_and_keeps_rows(self, tmp_path):
        db = tmp_path / "old.db"
        conn = sqlite3.connect(db)
        conn.execute("""CREATE TABLE prices (
            id INTEGER PRIMARY KEY, vendor TEXT NOT NULL, service TEXT NOT NULL,
            region TEXT NOT NULL, product_family TEXT, attributes TEXT, attributes_hash TEXT,
            usage_metric TEXT NOT NULL, unit TEXT NOT NULL, price_usd REAL NOT NULL,
            start_usage_amount REAL, end_usage_amount REAL, purchase_option TEXT,
            effective_date TEXT, source TEXT NOT NULL, fetched_at TEXT NOT NULL, per TEXT)""")
        conn.execute("""INSERT INTO prices (vendor, service, region, usage_metric, unit,
            price_usd, source, fetched_at) VALUES ('aws','S','r','m','u',2.0,'infracost','now')""")
        conn.commit()
        conn.close()
        cache = PricingCache(db, seed=False)
        price = cache.query("aws", "S", "r", "m")
        assert price.price_usd == 2.0
        assert price.block_size is None
