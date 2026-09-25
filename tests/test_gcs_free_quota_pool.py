"""The Cloud Storage free quotas apply once to three regions together (#402).

https://cloud.google.com/storage/pricing: "Cloud Storage Always Free quotas
apply to usage in US-WEST1, US-CENTRAL1, and US-EAST1 regions. Usage is
aggregated across these 3 regions." The quotas are 5 GB-months of Standard
storage, 5,000 Class A operations and 50,000 Class B operations a month. A
model with a bucket in us-central1 and one in us-east1 gets 5 free GB-months
in all, not 5 in each region. Each region still pays its own rate for its
paid use, because storage prices differ by region.
"""

import warnings

import pytest

from infra_cost_model.engine import CostEngine
from infra_cost_model.pricing.cache import Price
from infra_cost_model.pricing.catalog import PricingCatalog

STORAGE = ("gcp", "CloudStorage", "GCS-Standard-GiB-Month", "GiB-month")
CLASS_A = ("gcp", "CloudStorage", "GCS-Class-A-Operation", "operations")

# Test rates: us-east1 costs more than us-central1, so a test can tell
# which region's rate a node pays.
STORAGE_ROWS = {
    "us-central1": ((0.0, 0, 5), (0.020, 5, None)),
    "us-east1": ((0.0, 0, 5), (0.026, 5, None)),
    "us-west1": ((0.0, 0, 5), (0.020, 5, None)),
    "europe-west1": ((0.020, 0, None),),
}
CLASS_A_ROWS = {
    "us-central1": ((0.0, 0, 5000), (0.000005, 5000, None)),
    "us-east1": ((0.0, 0, 5000), (0.000006, 5000, None)),
}


def _catalog(tmp_path):
    catalog = PricingCatalog(db_path=tmp_path / "pricing.db")
    for metric, rows_by_region in ((STORAGE, STORAGE_ROWS),
                                   (CLASS_A, CLASS_A_ROWS)):
        vendor, service, usage_metric, unit = metric
        for region, tiers in rows_by_region.items():
            for price, start, end in tiers:
                catalog._cache.upsert(Price(
                    vendor=vendor, service=service, region=region,
                    product_family="", attributes={},
                    usage_metric=usage_metric, unit=unit, price_usd=price,
                    start_usage_amount=start, end_usage_amount=end,
                    source="test", fetched_at="2026-01-01T00:00:00",
                ))
    return catalog


def node(metric, region, quantity):
    vendor, service, usage_metric, unit = metric
    return {
        "nodeType": "external",
        "provider": vendor,
        "service": service,
        "region": region,
        "usageMetrics": {usage_metric: {"unit": unit, "value": quantity,
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


def test_two_free_regions_share_one_storage_quota(tmp_path):
    # 6 GB-months in all, 5 free: each region pays for its 0.5 at its rate.
    costs = compute(_catalog(tmp_path), {
        "iowa": node(STORAGE, "us-central1", 3),
        "carolina": node(STORAGE, "us-east1", 3)})
    assert costs["iowa"] == pytest.approx(0.5 * 0.020)
    assert costs["carolina"] == pytest.approx(0.5 * 0.026)


def test_three_free_regions_share_one_storage_quota(tmp_path):
    costs = compute(_catalog(tmp_path), {
        "iowa": node(STORAGE, "us-central1", 2),
        "carolina": node(STORAGE, "us-east1", 2),
        "oregon": node(STORAGE, "us-west1", 2)})
    paid = 2 * (1 - 5 / 6)
    assert costs["iowa"] == pytest.approx(paid * 0.020)
    assert costs["carolina"] == pytest.approx(paid * 0.026)
    assert costs["oregon"] == pytest.approx(paid * 0.020)


def test_use_under_the_quota_stays_free(tmp_path):
    costs = compute(_catalog(tmp_path), {
        "iowa": node(STORAGE, "us-central1", 2),
        "carolina": node(STORAGE, "us-east1", 2)})
    assert costs["iowa"] == pytest.approx(0.0)
    assert costs["carolina"] == pytest.approx(0.0)


def test_two_free_regions_share_one_operation_quota(tmp_path):
    costs = compute(_catalog(tmp_path), {
        "iowa": node(CLASS_A, "us-central1", 3000),
        "carolina": node(CLASS_A, "us-east1", 3000)})
    assert costs["iowa"] == pytest.approx(500 * 0.000005)
    assert costs["carolina"] == pytest.approx(500 * 0.000006)


def test_other_region_does_not_use_the_quota(tmp_path):
    costs = compute(_catalog(tmp_path), {
        "iowa": node(STORAGE, "us-central1", 3),
        "belgium": node(STORAGE, "europe-west1", 3)})
    assert costs["iowa"] == pytest.approx(0.0)
    assert costs["belgium"] == pytest.approx(3 * 0.020)


def test_one_region_is_unchanged(tmp_path):
    costs = compute(_catalog(tmp_path), {
        "iowa": node(STORAGE, "us-central1", 8)})
    assert costs["iowa"] == pytest.approx(3 * 0.020)
