"""One free allowance can cover several usage metrics (#338).

AWS gives the account 1,000,000 free SQS requests a month, for standard and
FIFO queues together. The AWS price list states it as one product,
"Global-Requests", with location "Any" and queue type "Any". The engine adds
up the monthly quantity of every metric in the group, in every region,
applies the allowance once, and splits it across the metrics and nodes in
proportion to their quantities. Each metric pays its own rate, in its own
region, for the rest.
"""

import warnings

import pytest

from infra_cost_model.engine import CostEngine
from infra_cost_model.pricing.cache import Price
from infra_cost_model.pricing.catalog import PricingCatalog
from infra_cost_model.pricing.free_tiers import (
    ACCOUNT, SHARED_FREE_ALLOWANCES, free_tier_scope, shared_free_allowance,
)

STANDARD = "SQS-Standard-Request"
FIFO = "SQS-FIFO-Request"
SEED_RATE = {STANDARD: 0.0000004, FIFO: 0.0000005}

# Paid rates for a two-region test catalog. They differ by region so that
# the tests can tell which region's rate priced each quantity.
RATES = {
    ("us-east-1", STANDARD): 0.0000004,
    ("us-east-1", FIFO): 0.0000005,
    ("eu-west-1", STANDARD): 0.0000008,
    ("eu-west-1", FIFO): 0.0000010,
}


def queue(metric, requests, region="us-east-1"):
    return {
        "nodeType": "queue",
        "provider": "aws",
        "service": "AmazonSQS",
        "region": region,
        "usageMetrics": {metric: {"unit": "requests", "value": requests,
                                  "fixed": True}},
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


def _row(region, metric, price, start, end):
    return Price(
        vendor="aws", service="AmazonSQS", region=region, product_family="",
        attributes={}, usage_metric=metric, unit="requests", price_usd=price,
        start_usage_amount=start, end_usage_amount=end, source="test",
        fetched_at="2026-01-01T00:00:00",
    )


def _catalog(path, free_rows):
    catalog = PricingCatalog(db_path=path / "pricing.db")
    for (region, metric), rate in RATES.items():
        if free_rows:
            catalog._cache.upsert(_row(region, metric, 0.0, 0, 1_000_000))
            catalog._cache.upsert(_row(region, metric, rate, 1_000_000, None))
        else:
            catalog._cache.upsert(_row(region, metric, rate, 0, None))
    return catalog


@pytest.fixture
def two_regions(tmp_path):
    """Rows like the seed file: each metric states its own free tier."""
    return _catalog(tmp_path, free_rows=True)


@pytest.fixture
def price_list_rows(tmp_path):
    """Rows like the AWS price list: the paid rates start at 0, and the
    free allowance is a separate product that maps to no metric."""
    return _catalog(tmp_path, free_rows=False)


def test_table_groups_standard_and_fifo_requests():
    group = shared_free_allowance("aws", "AmazonSQS", STANDARD)
    assert group is not None
    assert group is shared_free_allowance("aws", "AmazonSQS", FIFO)
    assert group.metrics == frozenset({STANDARD, FIFO})
    assert group.allowance == 1_000_000
    assert group.unit == "requests"


def test_separate_allowances_stay_separate():
    for service, metric in [("AWSLambda", "Lambda-Request"),
                            ("AWSLambda", "Lambda-GB-Second"),
                            ("AmazonSNS", "SNS-Publish"),
                            ("AmazonSNS", "SNS-Delivery-HTTP")]:
        assert shared_free_allowance("aws", service, metric) is None, metric


def test_grouped_metrics_are_account_wide():
    for group in SHARED_FREE_ALLOWANCES:
        for metric in group.metrics:
            assert free_tier_scope(group.vendor, group.service, metric) == ACCOUNT


# The seed file prices each group's service in one region.
SEED_REGION = {"AmazonSQS": "us-east-1", "AmazonCloudFront": "global"}


def test_seed_rows_use_the_group_unit(seed_catalog):
    for group in SHARED_FREE_ALLOWANCES:
        for metric in group.metrics:
            tiers = seed_catalog.query(group.vendor, group.service,
                                       SEED_REGION[group.service], metric).tiers
            assert {t.unit for t in tiers} == {group.unit}, metric


def test_standard_only(seed_catalog):
    costs = compute(seed_catalog, {"q": queue(STANDARD, 1_500_000)})
    assert costs["q"] == pytest.approx(500_000 * SEED_RATE[STANDARD], rel=1e-9)


def test_fifo_only(seed_catalog):
    costs = compute(seed_catalog, {"q": queue(FIFO, 1_500_000)})
    assert costs["q"] == pytest.approx(500_000 * SEED_RATE[FIFO], rel=1e-9)


def test_standard_and_fifo_share_one_allowance(seed_catalog):
    # 600,000 + 600,000 requests: 1,000,000 free for the account, 500,000
    # of it for each metric, so each pays for 100,000 at its own rate.
    costs = compute(seed_catalog, {"std": queue(STANDARD, 600_000),
                                   "fifo": queue(FIFO, 600_000)})
    assert costs["std"] == pytest.approx(100_000 * SEED_RATE[STANDARD], rel=1e-9)
    assert costs["fifo"] == pytest.approx(100_000 * SEED_RATE[FIFO], rel=1e-9)


def test_issue_example_bills_one_million_requests(seed_catalog):
    costs = compute(seed_catalog, {"std": queue(STANDARD, 1_000_000),
                                   "fifo": queue(FIFO, 1_000_000)})
    assert costs["std"] == pytest.approx(500_000 * SEED_RATE[STANDARD], rel=1e-9)
    assert costs["fifo"] == pytest.approx(500_000 * SEED_RATE[FIFO], rel=1e-9)


def test_allowance_splits_in_proportion_to_quantity(seed_catalog):
    # 300,000 + 900,000: the metrics get 250,000 and 750,000 free.
    costs = compute(seed_catalog, {"std": queue(STANDARD, 300_000),
                                   "fifo": queue(FIFO, 900_000)})
    assert costs["std"] == pytest.approx(50_000 * SEED_RATE[STANDARD], rel=1e-9)
    assert costs["fifo"] == pytest.approx(150_000 * SEED_RATE[FIFO], rel=1e-9)


def test_nodes_of_one_metric_split_its_cost(seed_catalog):
    costs = compute(seed_catalog, {"a": queue(STANDARD, 300_000),
                                   "b": queue(STANDARD, 300_000),
                                   "fifo": queue(FIFO, 600_000)})
    assert costs["a"] == pytest.approx(50_000 * SEED_RATE[STANDARD], rel=1e-9)
    assert costs["b"] == pytest.approx(50_000 * SEED_RATE[STANDARD], rel=1e-9)
    assert costs["fifo"] == pytest.approx(100_000 * SEED_RATE[FIFO], rel=1e-9)


def test_usage_under_the_allowance_is_free(seed_catalog):
    costs = compute(seed_catalog, {"std": queue(STANDARD, 400_000),
                                   "fifo": queue(FIFO, 400_000)})
    assert costs["std"] == pytest.approx(0.0, abs=1e-12)
    assert costs["fifo"] == pytest.approx(0.0, abs=1e-12)


def test_one_allowance_across_metrics_and_regions(two_regions):
    # 4 pools of 300,000 requests: 1,200,000 in all, 200,000 billed. Each
    # pool gets 250,000 free and pays for 50,000 at its own rate.
    nodes = {f"{region}-{metric}": queue(metric, 300_000, region)
             for region, metric in RATES}
    costs = compute(two_regions, nodes)
    for (region, metric), rate in RATES.items():
        assert costs[f"{region}-{metric}"] == pytest.approx(50_000 * rate,
                                                            rel=1e-9)


def test_rows_without_a_free_tier_get_the_group_allowance(price_list_rows):
    costs = compute(price_list_rows, {"std": queue(STANDARD, 600_000),
                                      "fifo": queue(FIFO, 600_000,
                                                    "eu-west-1")})
    assert costs["std"] == pytest.approx(
        100_000 * RATES[("us-east-1", STANDARD)], rel=1e-9)
    assert costs["fifo"] == pytest.approx(
        100_000 * RATES[("eu-west-1", FIFO)], rel=1e-9)


def test_shared_total_is_the_same_in_every_time_basis(seed_catalog):
    nodes = {"std": queue(STANDARD, 600_000), "fifo": queue(FIFO, 600_000)}
    monthly = sum(compute(seed_catalog, nodes, "monthly").values())
    yearly = sum(compute(seed_catalog, nodes, "yearly").values())
    assert monthly == pytest.approx(0.04 + 0.05, rel=1e-9)
    assert yearly == pytest.approx(12 * monthly, rel=1e-9)


def test_two_workflows_share_the_allowance(seed_catalog):
    # Usage-driven requests from 2 workflows, one per queue type.
    m = {
        "version": "1.0",
        "workflows": [
            {"name": "a", "entry": "std",
             "frequency": {"unit": "perMonth", "value": 600_000}},
            {"name": "b", "entry": "fifo",
             "frequency": {"unit": "perMonth", "value": 600_000}},
        ],
        "nodes": {
            name: {"nodeType": "queue", "provider": "aws",
                   "service": "AmazonSQS", "region": "us-east-1",
                   "usageMetrics": {metric: {"unit": "requests", "value": 1}}}
            for name, metric in [("std", STANDARD), ("fifo", FIFO)]
        },
        "edges": [],
    }
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        costs = CostEngine(m, catalog=seed_catalog,
                           time_basis="monthly").compute()
    assert costs["std"] == pytest.approx(0.04, rel=1e-9)
    assert costs["fifo"] == pytest.approx(0.05, rel=1e-9)


# CloudFront gives 10,000,000 free HTTP or HTTPS requests a month (#339).
HTTP = "CloudFront-HTTP-Request"
HTTPS = "CloudFront-HTTPS-Request"


def distribution(http, https):
    return {
        "nodeType": "routing",
        "provider": "aws",
        "service": "AmazonCloudFront",
        "region": "global",
        "usageMetrics": {
            HTTP: {"unit": "requests", "value": http, "fixed": True},
            HTTPS: {"unit": "requests", "value": https, "fixed": True},
        },
    }


def test_table_groups_cloudfront_http_and_https_requests():
    group = shared_free_allowance("aws", "AmazonCloudFront", HTTP)
    assert group is not None
    assert group is shared_free_allowance("aws", "AmazonCloudFront", HTTPS)
    assert group.metrics == frozenset({HTTP, HTTPS})
    assert group.allowance == 10_000_000
    assert shared_free_allowance("aws", "AmazonCloudFront",
                                 "CloudFront-DataTransfer") is None


def test_cloudfront_http_and_https_share_one_allowance(seed_catalog):
    # 8,000,000 + 8,000,000 requests: 6,000,000 billed, 3,000,000 of each,
    # $2.25 of HTTP at $0.0075 per 10,000 and $3.00 of HTTPS at $0.01.
    costs = compute(seed_catalog, {"cdn": distribution(8_000_000, 8_000_000)})
    assert costs["cdn"] == pytest.approx(5.25, rel=1e-9)


@pytest.mark.parametrize("http, https", [
    (8_000_000, 8_000_000), (2_000_000, 6_000_000), (0, 25_000_000),
    (3_000_000, 30_000_000),
])
def test_engine_agrees_with_the_cloudfront_helper(seed_catalog, http, https):
    from infra_cost_model.resources.cloudfront import _cloudfront_cost

    requests = http + https
    helper = _cloudfront_cost(requests=requests, https_ratio=https / requests,
                              catalog=seed_catalog, region="global")
    costs = compute(seed_catalog, {"cdn": distribution(http, https)})
    assert costs["cdn"] == pytest.approx(helper, rel=1e-9)
