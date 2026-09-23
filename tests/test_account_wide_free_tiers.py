"""Account-wide free allowances apply once across regions (#336).

AWS gives some free allowances to the whole account, across every region:
the first 100 GB a month of data transfer out, and Lambda's 1,000,000
requests and 400,000 GB-seconds. The engine adds up each region's monthly
quantity, applies the allowance once, and splits it across the regions in
proportion to their quantities. It prices each region's paid usage at that
region's own rate.

The seed file has us-east-1 prices only, so these tests build a small
catalog with us-east-1 and eu-west-1 rows.
"""

import warnings

import pytest

from infra_cost_model.engine import CostEngine
from infra_cost_model.pricing.cache import Price
from infra_cost_model.pricing.catalog import PricingCatalog
from infra_cost_model.pricing.free_tiers import (
    ACCOUNT, REGION, free_tier_scope,
)

EGRESS = ("AWSDataTransfer", "DataTransfer-Internet-Out-GB", "GB")
REGIONAL = ("TestRegionalService", "Regional-Metric", "GB")

# us-east-1 and eu-west-1 paid egress rates. They differ here so that the
# tests can tell which region's rate priced each quantity.
EGRESS_RATE = {"us-east-1": 0.09, "eu-west-1": 0.10}


def _row(region, service, metric, unit, price, start, end):
    return Price(
        vendor="aws", service=service, region=region, product_family="",
        attributes={}, usage_metric=metric, unit=unit, price_usd=price,
        start_usage_amount=start, end_usage_amount=end, source="test",
        fetched_at="2026-01-01T00:00:00",
    )


@pytest.fixture
def catalog(tmp_path):
    catalog = PricingCatalog(db_path=tmp_path / "pricing.db")
    cache = catalog._cache
    for region, rate in EGRESS_RATE.items():
        service, metric, unit = EGRESS
        cache.upsert(_row(region, service, metric, unit, 0.0, 0, 100))
        cache.upsert(_row(region, service, metric, unit, rate, 100, None))
        cache.upsert(_row(region, "AWSLambda", "Lambda-Request", "requests",
                          0.0, 0, 1_000_000))
        cache.upsert(_row(region, "AWSLambda", "Lambda-Request", "requests",
                          0.0000002, 1_000_000, None))
        service, metric, unit = REGIONAL
        cache.upsert(_row(region, service, metric, unit, 0.0, 0, 100))
        cache.upsert(_row(region, service, metric, unit, 1.0, 100, None))
    return catalog


def node(region, quantity, service=EGRESS[0], metric=EGRESS[1], unit=EGRESS[2]):
    return {
        "nodeType": "external",
        "provider": "aws",
        "service": service,
        "region": region,
        "usageMetrics": {metric: {"unit": unit, "value": quantity, "fixed": True}},
    }


def model(nodes):
    names = list(nodes)
    return {
        "version": "1.0",
        "workflow": {"name": "w", "entry": names[0],
                     "frequency": {"unit": "perMonth", "value": 1}},
        "nodes": nodes,
        "edges": [{"from": a, "to": b, "type": "async", "rate": 1}
                  for a, b in zip(names, names[1:])],
    }


def compute(catalog, nodes, time_basis="monthly"):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return CostEngine(model(nodes), catalog=catalog,
                          time_basis=time_basis).compute()


def test_scope_table_marks_the_account_wide_allowances():
    for service, metric in [
        ("AWSDataTransfer", "DataTransfer-Internet-Out-GB"),
        ("AWSLambda", "Lambda-Request"),
        ("AWSLambda", "Lambda-GB-Second"),
        ("AmazonSQS", "SQS-Standard-Request"),
        ("AmazonSQS", "SQS-FIFO-Request"),
        ("AmazonSNS", "SNS-Publish"),
        ("AmazonSNS", "SNS-Delivery-HTTP"),
    ]:
        assert free_tier_scope("aws", service, metric) == ACCOUNT, metric
    assert free_tier_scope("aws", "AmazonS3", "S3-Storage") == REGION
    assert free_tier_scope("github", "Copilot", "Copilot-Credit") == REGION


def test_two_regions_share_one_egress_allowance(catalog):
    # 60 GB + 60 GB: 100 GB free for the account, 50 GB of it for each
    # region, so each region pays for 10 GB at its own rate.
    costs = compute(catalog, {"us": node("us-east-1", 60),
                              "eu": node("eu-west-1", 60)})
    assert costs["us"] == pytest.approx(10 * 0.09, rel=1e-9)
    assert costs["eu"] == pytest.approx(10 * 0.10, rel=1e-9)


def test_allowance_splits_in_proportion_to_region_quantity(catalog):
    # 30 GB + 90 GB: the regions get 25 GB and 75 GB of the allowance.
    costs = compute(catalog, {"us": node("us-east-1", 30),
                              "eu": node("eu-west-1", 90)})
    assert costs["us"] == pytest.approx(5 * 0.09, rel=1e-9)
    assert costs["eu"] == pytest.approx(15 * 0.10, rel=1e-9)


def test_usage_under_the_allowance_is_free_in_every_region(catalog):
    costs = compute(catalog, {"us": node("us-east-1", 40),
                              "eu": node("eu-west-1", 40)})
    assert costs["us"] == pytest.approx(0.0, abs=1e-12)
    assert costs["eu"] == pytest.approx(0.0, abs=1e-12)


def test_nodes_in_one_region_split_that_region_cost(catalog):
    # us-east-1 has 30 GB + 30 GB, eu-west-1 has 60 GB: the same regional
    # totals as the first test, and the two us-east-1 nodes share 0.90.
    costs = compute(catalog, {"us1": node("us-east-1", 30),
                              "us2": node("us-east-1", 30),
                              "eu": node("eu-west-1", 60)})
    assert costs["us1"] == pytest.approx(5 * 0.09, rel=1e-9)
    assert costs["us2"] == pytest.approx(5 * 0.09, rel=1e-9)
    assert costs["eu"] == pytest.approx(10 * 0.10, rel=1e-9)


def test_lambda_requests_share_one_allowance_across_regions(catalog):
    lam = {"service": "AWSLambda", "metric": "Lambda-Request", "unit": "requests"}
    costs = compute(catalog, {"us": node("us-east-1", 1_500_000, **lam),
                              "eu": node("eu-west-1", 1_500_000, **lam)})
    # 3,000,000 requests, 1,000,000 of them free for the account: each
    # region pays for 1,000,000. Separate allowances would bill 500,000 each.
    assert costs["us"] == pytest.approx(1_000_000 * 0.0000002, rel=1e-9)
    assert costs["eu"] == pytest.approx(1_000_000 * 0.0000002, rel=1e-9)


def test_regional_allowance_applies_once_per_region(catalog):
    service, metric, unit = REGIONAL
    costs = compute(catalog, {
        "us": node("us-east-1", 60, service=service, metric=metric, unit=unit),
        "eu": node("eu-west-1", 60, service=service, metric=metric, unit=unit),
    })
    assert costs["us"] == pytest.approx(0.0, abs=1e-12)
    assert costs["eu"] == pytest.approx(0.0, abs=1e-12)


def test_single_region_pool_is_unchanged(catalog):
    two = compute(catalog, {"a": node("us-east-1", 60),
                            "b": node("us-east-1", 60)})
    assert two["a"] == pytest.approx(10 * 0.09, rel=1e-9)
    assert two["b"] == pytest.approx(10 * 0.09, rel=1e-9)
    one = compute(catalog, {"a": node("us-east-1", 120)})
    assert one["a"] == pytest.approx(20 * 0.09, rel=1e-9)


def test_shared_allowance_total_is_the_same_in_every_time_basis(catalog):
    nodes = {"us": node("us-east-1", 60), "eu": node("eu-west-1", 60)}
    monthly = sum(compute(catalog, nodes, "monthly").values())
    yearly = sum(compute(catalog, nodes, "yearly").values())
    assert monthly == pytest.approx(10 * 0.09 + 10 * 0.10, rel=1e-9)
    assert yearly == pytest.approx(12 * monthly, rel=1e-9)
