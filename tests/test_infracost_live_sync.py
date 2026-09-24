"""A live sync replaces a metric's Infracost rows and keeps its free tier.

#355: a sync used to add rows and never delete them, so rows from a product
that a descriptor no longer selects stayed next to the new ones, and a query
charged the quantity once for each product.

#356: Infracost states the paid prices of a metric from 0, with no $0 tier.
The AWS free allowances are separate "Global-" products that no descriptor
selects. A live sync now adds the allowance from ``FREE_ALLOWANCES`` as a $0
tier, so a live catalog prices the same usage as the seed catalog does.

The fake Cloud Pricing API below returns the tiers that the live API
returned for us-east-1 on 2026-09-24, with "Inf" as the end of the last
tier.
"""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from infra_cost_model.pricing.cache import load_seed_rows
from infra_cost_model.pricing.catalog import PricingCatalog
from infra_cost_model.pricing.free_tiers import FREE_ALLOWANCES
from infra_cost_model.pricing.sources import infracost as ic


def _product(family, attributes, prices):
    return {
        "productFamily": family,
        "attributes": [{"key": k, "value": v} for k, v in
                       {**attributes, "regionCode": "us-east-1"}.items()],
        "prices": [
            {"USD": usd, "unit": unit, "startUsageAmount": start, "endUsageAmount": end}
            for usd, unit, start, end in prices
        ],
    }


US_EAST_1 = [
    _product("Serverless", {"group": "AWS-Lambda-Requests"},
             [("0.0000002", "Requests", "0", "Inf")]),
    _product("Serverless", {"group": "AWS-Lambda-Duration"},
             [("0.0000166667", "seconds", "0", "6000000000"),
              ("0.000015", "seconds", "6000000000", "15000000000"),
              ("0.0000133334", "seconds", "15000000000", "Inf")]),
    _product("Metric", {"usagetype": "CW:MetricMonitorUsage"},
             [("0.3", "Metrics", "0", "10000"), ("0.1", "Metrics", "10000", "250000"),
              ("0.05", "Metrics", "250000", "1000000"),
              ("0.02", "Metrics", "1000000", "Inf")]),
    _product("Alarm", {"usagetype": "CW:AlarmMonitorUsage"},
             [("0.1", "Alarms", "0", "Inf")]),
    _product("Data Payload", {"usagetype": "USE1-DataProcessing-Bytes"},
             [("0.5", "GB", "0", "Inf")]),
    _product("Data Payload", {"usagetype": "USE1-CentralizedBytes"},
             [("0.05", "GB", "0", "Inf")]),
    _product("Storage Snapshot", {"usagetype": "USE1-TimedStorage-ByteHrs"},
             [("0.03", "GB-Mo", "0", "Inf")]),
]


def _fake_post(catalogue):
    """Return a `requests.post` stand-in that filters *catalogue* like the API."""
    def post(url, headers=None, json=None, timeout=None):
        variables = json["variables"]
        family = variables.get("productFamily")
        filters = variables.get("attributeFilters") or []
        matched = []
        for product in catalogue:
            attrs = {a["key"]: a["value"] for a in product["attributes"]}
            if family and product["productFamily"] != family:
                continue
            if all(attrs.get(f["key"]) == f["value"] for f in filters):
                matched.append(product)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"data": {"products": matched}}
        resp.raise_for_status.return_value = None
        return resp
    return post


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("INFRACOST_API_KEY", "test-token")
    monkeypatch.setenv("INFRACOST_ORG_ID", "org-123")


def _sync(catalog, metrics, catalogue=US_EAST_1):
    client = ic.InfracostClient()
    with patch.object(ic.requests, "post", side_effect=_fake_post(catalogue)):
        for metric in metrics:
            client.sync_to_cache(catalog._cache, metric, "us-east-1")


def _rows(catalog, metric, source):
    conn = sqlite3.connect(catalog._cache.db_path)
    try:
        return conn.execute(
            "SELECT attributes, price_usd, start_usage_amount FROM prices "
            "WHERE usage_metric = ? AND region = 'us-east-1' AND source = ? "
            "ORDER BY start_usage_amount", (metric, source)).fetchall()
    finally:
        conn.close()


# --- #355: a sync replaces the metric's live rows -------------------------------

OLD_STORAGE_DESCRIPTOR = {
    "service": "AmazonCloudWatch",
    "attribute_filters": [{"key": "usagetype", "value": "REGION_PREFIX-CentralizedBytes"}],
    "unit": "GB",
}
STORAGE = "CloudWatch-Log-Storage"


def test_resync_after_descriptor_change_keeps_only_the_new_product(creds, tmp_path,
                                                                    monkeypatch):
    catalog = PricingCatalog(db_path=tmp_path / "pricing.db")
    new_descriptor = ic.METRIC_DESCRIPTORS[STORAGE]
    monkeypatch.setitem(ic.METRIC_DESCRIPTORS, STORAGE, OLD_STORAGE_DESCRIPTOR)
    _sync(catalog, [STORAGE])
    monkeypatch.setitem(ic.METRIC_DESCRIPTORS, STORAGE, new_descriptor)
    _sync(catalog, [STORAGE])

    rows = _rows(catalog, STORAGE, "infracost")
    assert all("TimedStorage" in attrs for attrs, _, _ in rows)
    assert [(price, start) for _, price, start in rows] == [(0.0, 0.0), (0.03, 5.0)]
    # 10 GB-month: 5 free, then 5 at $0.03.
    result = catalog.query("aws", "AmazonCloudWatch", "us-east-1", STORAGE, 10)
    assert result.total_cost == pytest.approx(0.15)


def test_resync_leaves_seed_rows_and_other_regions(creds, tmp_path):
    catalog = PricingCatalog(db_path=tmp_path / "pricing.db", seed=True)
    seed_before = _rows(catalog, STORAGE, "seed")
    assert seed_before
    eu_storage = _product("Storage Snapshot", {"usagetype": "EU-TimedStorage-ByteHrs"},
                          [("0.03", "GB-Mo", "0", "Inf")])
    client = ic.InfracostClient()
    with patch.object(ic.requests, "post", side_effect=_fake_post([eu_storage])):
        client.sync_to_cache(catalog._cache, STORAGE, "eu-west-1")
    _sync(catalog, [STORAGE])
    _sync(catalog, [STORAGE])

    assert _rows(catalog, STORAGE, "seed") == seed_before
    conn = sqlite3.connect(catalog._cache.db_path)
    try:
        eu = conn.execute("SELECT COUNT(*) FROM prices WHERE usage_metric = ? "
                          "AND region = 'eu-west-1' AND source = 'infracost'",
                          (STORAGE,)).fetchone()[0]
    finally:
        conn.close()
    assert eu == 2
    assert len(_rows(catalog, STORAGE, "infracost")) == 2


def test_rejected_sync_keeps_the_previous_live_rows(creds, tmp_path, monkeypatch):
    """A sync that fails the one-product guard changes nothing."""
    catalog = PricingCatalog(db_path=tmp_path / "pricing.db")
    _sync(catalog, [STORAGE])
    before = _rows(catalog, STORAGE, "infracost")
    monkeypatch.setitem(ic.METRIC_DESCRIPTORS, STORAGE, {
        "service": "AmazonCloudWatch", "attribute_filters": [],
    })
    with pytest.raises(RuntimeError, match="must match one"):
        _sync(catalog, [STORAGE])
    assert _rows(catalog, STORAGE, "infracost") == before


def test_resync_with_no_matching_product_removes_the_live_rows(creds, tmp_path):
    """The metric then falls back to the seed rows."""
    catalog = PricingCatalog(db_path=tmp_path / "pricing.db", seed=True)
    _sync(catalog, [STORAGE])
    _sync(catalog, [STORAGE], catalogue=[])
    assert _rows(catalog, STORAGE, "infracost") == []
    result = catalog.query("aws", "AmazonCloudWatch", "us-east-1", STORAGE, 10)
    assert result.total_cost == pytest.approx(0.15)


# --- #356: the free allowances survive a live sync ------------------------------

# (service, metric): quantities below and above the free allowance, and
# below the first boundary where live and seed tiers differ.
QUANTITIES = {
    ("AWSLambda", "Lambda-Request"): [0, 400_000, 1_000_000, 3_500_000],
    ("AWSLambda", "Lambda-GB-Second"): [0, 100_000, 400_000, 2_000_000],
    ("AmazonCloudWatch", "CloudWatch-Metric-Month"): [0, 3, 10, 500, 20_000, 2_000_000],
    ("AmazonCloudWatch", "CloudWatch-Alarm-Month"): [0, 7, 10, 250],
    ("AmazonCloudWatch", "CloudWatch-Log-Ingestion"): [0, 3, 5, 10, 1_000],
    ("AmazonCloudWatch", "CloudWatch-Log-Storage"): [0, 3, 5, 10, 1_000],
}


@pytest.mark.parametrize("service,metric", sorted(QUANTITIES))
def test_live_sync_prices_usage_as_the_seed_does(creds, tmp_path, service, metric):
    seed = PricingCatalog(db_path=tmp_path / "seed.db", seed=True)
    live = PricingCatalog(db_path=tmp_path / "live.db", seed=True)
    _sync(live, [metric])
    assert _rows(live, metric, "infracost"), "the sync stored no live rows"

    for quantity in QUANTITIES[(service, metric)]:
        want = seed.query("aws", service, "us-east-1", metric, quantity)
        got = live.query("aws", service, "us-east-1", metric, quantity)
        assert got.total_cost == pytest.approx(want.total_cost), quantity
        assert got.free_allowance == want.free_allowance
        paid = live.query("aws", service, "us-east-1", metric, quantity,
                          include_free_tier=False)
        seed_paid = seed.query("aws", service, "us-east-1", metric, quantity,
                               include_free_tier=False)
        assert paid.total_cost == pytest.approx(seed_paid.total_cost), quantity


def test_log_ingestion_of_3_gb_is_free_after_a_live_sync(creds, tmp_path):
    catalog = PricingCatalog(db_path=tmp_path / "pricing.db")
    _sync(catalog, ["CloudWatch-Log-Ingestion"])
    result = catalog.query("aws", "AmazonCloudWatch", "us-east-1",
                           "CloudWatch-Log-Ingestion", 3)
    assert result.total_cost == 0


def test_free_allowances_match_the_seed_free_tiers():
    """Each $0 tier from 0 in the seed file has an entry, and the other way round."""
    seed = {
        (r.vendor, r.service, r.usage_metric): r.end_usage_amount
        for r in load_seed_rows()
        if r.price_usd == 0 and r.start_usage_amount == 0
    }
    assert FREE_ALLOWANCES == seed


def test_sync_adds_the_free_tier_once(creds, tmp_path):
    """A second sync writes the same rows, not a second free tier."""
    catalog = PricingCatalog(db_path=tmp_path / "pricing.db")
    _sync(catalog, ["CloudWatch-Metric-Month"])
    _sync(catalog, ["CloudWatch-Metric-Month"])
    rows = _rows(catalog, "CloudWatch-Metric-Month", "infracost")
    assert [(price, start) for _, price, start in rows] == [
        (0.0, 0.0), (0.3, 10.0), (0.1, 10_000.0), (0.05, 250_000.0), (0.02, 1_000_000.0)]
