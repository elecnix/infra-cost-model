"""A global service prices its tiers once for the whole account (#378).

AWS bills Route 53 hosted zones once per account: $0.50 a month for each
of the first 25 zones, then $0.10 (https://aws.amazon.com/route53/pricing/).
The AWS price list gives the ``HostedZone`` product the location "Any". A
model with 20 zones on a us-east-1 node and 20 on an eu-west-1 node costs
25 x $0.50 + 15 x $0.10 = $14.00 a month.

The engine adds up the quantity of a global metric in every region, prices
the total once from the "global" or us-east-1 rows, and splits the cost
across the nodes by quantity.
"""

import warnings

import pytest

from infra_cost_model.engine import CostEngine
from infra_cost_model.pricing.cache import Price
from infra_cost_model.pricing.catalog import PricingCatalog
from infra_cost_model.pricing.free_tiers import (
    ACCOUNT_WIDE_FREE_TIERS, SHARED_FREE_ALLOWANCES,
)
from infra_cost_model.pricing.global_services import (
    GLOBAL_METRICS, is_global_metric,
)

ZONES = ("AmazonRoute53", "Route53-HostedZone", "Zones")


def _row(region, price, start, end, service=ZONES[0], metric=ZONES[1]):
    return Price(
        vendor="aws", service=service, region=region, product_family="",
        attributes={}, usage_metric=metric, unit=ZONES[2], price_usd=price,
        start_usage_amount=start, end_usage_amount=end, source="test",
        fetched_at="2026-01-01T00:00:00",
    )


def _catalog(tmp_path, rates):
    """A catalog with the two hosted-zone tiers in each region of ``rates``."""
    catalog = PricingCatalog(db_path=tmp_path / "pricing.db")
    for region, (first, after) in rates.items():
        catalog._cache.upsert(_row(region, first, 0, 25))
        catalog._cache.upsert(_row(region, after, 25, None))
    return catalog


@pytest.fixture
def catalog(tmp_path):
    return _catalog(tmp_path, {"us-east-1": (0.50, 0.10),
                               "eu-west-1": (0.50, 0.10)})


def node(region, zones):
    return {
        "nodeType": "external",
        "provider": "aws",
        "service": ZONES[0],
        "region": region,
        "usageMetrics": {ZONES[1]: {"unit": ZONES[2], "value": zones,
                                    "fixed": True}},
    }


def compute(catalog, nodes):
    names = list(nodes)
    model = {
        "version": "1.0",
        "workflow": {"name": "w", "entry": names[0],
                     "frequency": {"unit": "perMonth", "value": 1}},
        "nodes": nodes,
        "edges": [{"from": a, "to": b, "type": "async", "rate": 1}
                  for a, b in zip(names, names[1:])],
    }
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return CostEngine(model, catalog=catalog,
                          time_basis="monthly").compute()


def test_table_marks_route53_hosted_zones_global():
    assert is_global_metric("aws", "AmazonRoute53", "Route53-HostedZone")
    assert not is_global_metric("aws", "AmazonS3", "S3-Storage")
    assert not is_global_metric("gcp", "AmazonRoute53", "Route53-HostedZone")


def test_global_metrics_are_not_in_the_allowance_tables():
    # A global metric's pool already applies its free tier once, so the
    # account-wide tables must not apply it a second time.
    shared = {(g.vendor, g.service, m)
              for g in SHARED_FREE_ALLOWANCES for m in g.metrics}
    for key in GLOBAL_METRICS:
        assert key not in ACCOUNT_WIDE_FREE_TIERS
        assert key not in shared


def test_two_regions_price_hosted_zones_once(catalog):
    costs = compute(catalog, {"us": node("us-east-1", 20),
                              "eu": node("eu-west-1", 20)})
    assert costs["us"] + costs["eu"] == pytest.approx(14.00, rel=1e-9)
    assert costs["us"] == pytest.approx(7.00, rel=1e-9)
    assert costs["eu"] == pytest.approx(7.00, rel=1e-9)


def test_split_follows_each_region_quantity(catalog):
    # 30 + 10 zones: $14.00 split 3:1.
    costs = compute(catalog, {"us": node("us-east-1", 30),
                              "eu": node("eu-west-1", 10)})
    assert costs["us"] == pytest.approx(10.50, rel=1e-9)
    assert costs["eu"] == pytest.approx(3.50, rel=1e-9)


def test_one_region_is_unchanged(catalog):
    assert compute(catalog, {"eu": node("eu-west-1", 20)})["eu"] == \
        pytest.approx(10.00, rel=1e-9)
    assert compute(catalog, {"us": node("us-east-1", 30)})["us"] == \
        pytest.approx(13.00, rel=1e-9)


def test_two_nodes_in_one_region_are_unchanged(catalog):
    costs = compute(catalog, {"a": node("eu-west-1", 20),
                              "b": node("eu-west-1", 20)})
    assert costs["a"] + costs["b"] == pytest.approx(14.00, rel=1e-9)


def test_pool_uses_the_us_east_1_rows(tmp_path):
    # When regional rows differ, the us-east-1 rows price the whole pool.
    catalog = _catalog(tmp_path, {"us-east-1": (0.50, 0.10),
                                  "eu-west-1": (0.90, 0.30)})
    costs = compute(catalog, {"us": node("us-east-1", 20),
                              "eu": node("eu-west-1", 20)})
    assert costs["us"] + costs["eu"] == pytest.approx(14.00, rel=1e-9)


def test_pool_prefers_the_global_rows(tmp_path):
    catalog = _catalog(tmp_path, {"global": (0.50, 0.10),
                                  "us-east-1": (0.90, 0.30),
                                  "eu-west-1": (0.90, 0.30)})
    costs = compute(catalog, {"us": node("us-east-1", 20),
                              "eu": node("eu-west-1", 20)})
    assert costs["us"] + costs["eu"] == pytest.approx(14.00, rel=1e-9)


def test_pool_without_global_rows_uses_a_node_region(tmp_path):
    catalog = _catalog(tmp_path, {"eu-west-1": (0.50, 0.10),
                                  "ap-south-1": (0.50, 0.10)})
    costs = compute(catalog, {"eu": node("eu-west-1", 20),
                              "ap": node("ap-south-1", 20)})
    assert costs["eu"] + costs["ap"] == pytest.approx(14.00, rel=1e-9)


def test_pool_region_choice_ignores_node_order(tmp_path):
    # Without global or us-east-1 rows, the rows of the region that comes
    # first in alphabetical order price the pool, whatever the node order.
    catalog = _catalog(tmp_path, {"eu-west-1": (0.50, 0.10),
                                  "ap-south-1": (0.90, 0.30)})
    expected = 25 * 0.90 + 15 * 0.30
    for nodes in ({"eu": node("eu-west-1", 20), "ap": node("ap-south-1", 20)},
                  {"ap": node("ap-south-1", 20), "eu": node("eu-west-1", 20)}):
        costs = compute(catalog, nodes)
        assert sum(costs.values()) == pytest.approx(expected, rel=1e-9)


def test_pool_is_skipped_when_no_region_prices_the_total(tmp_path):
    # A pool that no region can price keeps the costs of its regional pools.
    from infra_cost_model.engine.engine import (
        _CatalogCharge, _price_global_pools,
    )
    catalog = PricingCatalog(db_path=tmp_path / "pricing.db")
    pools = {
        ("aws", ZONES[0], region, ZONES[1], ()): [_CatalogCharge(
            node=region, pool=("aws", ZONES[0], region, ZONES[1], ()),
            quantity=20, cost=10.0, fixed=True, parameters={})]
        for region in ("us-east-1", "eu-west-1")
    }
    assert _price_global_pools(catalog, pools) == {}
