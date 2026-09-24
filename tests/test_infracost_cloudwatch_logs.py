"""Infracost descriptors for CloudWatch Logs select one AWS product each (#350).

The fake Cloud Pricing API below holds the CloudWatch Logs products that the
AWS price list for us-east-1 has (``AmazonCloudWatch``, published
2026-09-22), and answers a query the way the live API does: it keeps the
products whose product family and attributes equal the filter values. The
tests sync through that fake API and check which products reach the cache.
"""

from unittest.mock import MagicMock, patch

import pytest

from infra_cost_model.pricing.cache import Price, PricingCache, load_seed_rows
from infra_cost_model.pricing.sources import infracost as ic


def _product(usagetype, group, prices, family="Data Payload"):
    return {
        "productFamily": family,
        "attributes": [
            {"key": "usagetype", "value": usagetype},
            {"key": "group", "value": group},
            {"key": "regionCode", "value": "us-east-1"},
        ],
        "prices": [
            {"USD": usd, "unit": unit, "startUsageAmount": start, "endUsageAmount": end}
            for usd, unit, start, end in prices
        ],
    }


_VENDED_TIERS = [("0.5", "GB", "0", "10240"), ("0.25", "GB", "10240", "30720"),
                 ("0.1", "GB", "30720", "51200"), ("0.05", "GB", "51200", None)]

CLOUDWATCH_LOGS_US_EAST_1 = [
    _product("USE1-DataProcessing-Bytes", "Ingested Logs", [("0.5", "GB", "0", None)]),
    _product("DataProcessing-Bytes", "Ingested Logs", [("0.5", "GB", "0", None)]),
    _product("USE1-VendedLog-Bytes", "Ingested Logs", _VENDED_TIERS),
    _product("USE1-VendedLog-Bytes-MTLogs", "Ingested Logs", _VENDED_TIERS),
    _product("USE1-VendedLog-Bytes-CFLogs", "Ingested Logs", _VENDED_TIERS),
    _product("USE1-VendedLog-Bytes-WAFLogs", "Ingested Logs", _VENDED_TIERS),
    _product("USE1-CentralizedBytes", "Centralized Logs", [("0.05", "GB", "0", None)]),
    _product("USE1-TimedStorage-ByteHrs", "Amazon CloudWatch Standard Storage pricing current",
             [("0.03", "GB-Mo", "0", None)], family="Storage Snapshot"),
    _product("TimedStorage-ByteHrs", "Amazon CloudWatch Standard Storage pricing legacy",
             [("0.03", "GB-Mo", "0", None)], family="Storage Snapshot"),
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


def _sync(metric, catalogue=CLOUDWATCH_LOGS_US_EAST_1, region="us-east-1"):
    upserted = []
    cache = MagicMock()
    cache.upsert.side_effect = upserted.append
    with patch.object(ic.requests, "post", side_effect=_fake_post(catalogue)):
        n = ic.InfracostClient().sync_to_cache(cache, metric, region)
    assert n == len(upserted)
    return upserted


def _seed_paid_row(metric):
    rows = [r for r in load_seed_rows(["AmazonCloudWatch"])
            if r.usage_metric == metric and r.region == "us-east-1" and r.price_usd > 0]
    assert len(rows) == 1
    return rows[0]


def test_log_ingestion_stores_only_standard_ingestion(creds):
    rows = _sync("CloudWatch-Log-Ingestion")
    assert [r.attributes["usagetype"] for r in rows] == ["USE1-DataProcessing-Bytes"]
    seed = _seed_paid_row("CloudWatch-Log-Ingestion")
    assert rows[0].price_usd == pytest.approx(seed.price_usd) == pytest.approx(0.50)
    assert rows[0].unit == seed.unit == "GB"


def test_log_storage_stores_standard_storage_not_centralization(creds):
    rows = _sync("CloudWatch-Log-Storage")
    assert [r.attributes["usagetype"] for r in rows] == ["USE1-TimedStorage-ByteHrs"]
    seed = _seed_paid_row("CloudWatch-Log-Storage")
    assert rows[0].price_usd == pytest.approx(seed.price_usd) == pytest.approx(0.03)
    assert rows[0].unit == seed.unit == "GB-Mo"


@pytest.mark.parametrize("region,prefix", [("ca-central-1", "CAN1"), ("eu-west-1", "EU")])
def test_log_descriptors_resolve_region_prefix(creds, region, prefix):
    captured = []

    def post(url, headers=None, json=None, timeout=None):
        captured.append(json["variables"]["attributeFilters"])
        return _fake_post([])(url, headers, json, timeout)

    with patch.object(ic.requests, "post", side_effect=post):
        for metric in ("CloudWatch-Log-Ingestion", "CloudWatch-Log-Storage"):
            ic.InfracostClient().sync_to_cache(MagicMock(), metric, region)
    assert captured == [
        [{"key": "usagetype", "value": f"{prefix}-DataProcessing-Bytes"}],
        [{"key": "usagetype", "value": f"{prefix}-TimedStorage-ByteHrs"}],
    ]


def test_synced_log_prices_cost_10_gb(creds, tmp_path):
    """10 GB costs $5.00 to ingest and $0.30 a month to store, before the free tier."""
    cache = PricingCache(db_path=tmp_path / "pricing.db")
    client = ic.InfracostClient()
    with patch.object(ic.requests, "post", side_effect=_fake_post(CLOUDWATCH_LOGS_US_EAST_1)):
        client.sync_to_cache(cache, "CloudWatch-Log-Ingestion", "us-east-1")
        client.sync_to_cache(cache, "CloudWatch-Log-Storage", "us-east-1")
    for metric, expected in (("CloudWatch-Log-Ingestion", 5.00),
                             ("CloudWatch-Log-Storage", 0.30)):
        price = cache.query("aws", "AmazonCloudWatch", "us-east-1", metric)
        assert isinstance(price, Price)
        assert price.price_usd * 10 == pytest.approx(expected)


# --- Guard: one product per metric ---------------------------------------------


def test_sync_rejects_a_descriptor_that_matches_several_products(creds, monkeypatch):
    """A descriptor that matches several products would store overlapping tiers."""
    monkeypatch.setitem(ic.METRIC_DESCRIPTORS, "Test-Ingested-Logs", {
        "service": "AmazonCloudWatch",
        "attribute_filters": [{"key": "group", "value": "Ingested Logs"}],
        "unit": "GB",
    })
    cache = MagicMock()
    with patch.object(ic.requests, "post", side_effect=_fake_post(CLOUDWATCH_LOGS_US_EAST_1)):
        with pytest.raises(RuntimeError, match="matched 6 products") as exc:
            ic.InfracostClient().sync_to_cache(cache, "Test-Ingested-Logs", "us-east-1")
    assert "USE1-VendedLog-Bytes-CFLogs" in str(exc.value)
    cache.upsert.assert_not_called()


def test_sync_keeps_every_tier_of_one_product(creds):
    """Tiers of one product are not several products: all of them are stored."""
    products = [_product("CW:MetricMonitorUsage", "Metric", [
        ("0.3", "Metrics", "0", "10000"), ("0.1", "Metrics", "10000", "250000"),
        ("0.05", "Metrics", "250000", "1000000"), ("0.02", "Metrics", "1000000", None),
    ], family="Metric")]
    rows = _sync("CloudWatch-Metric-Month", catalogue=products)
    assert [r.price_usd for r in rows] == pytest.approx([0.3, 0.1, 0.05, 0.02])


def test_guard_failure_is_reported_by_sync_pricing_catalog(creds, monkeypatch, tmp_path):
    """sync_pricing_catalog reports the rejected metric in its warning."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setitem(ic.METRIC_DESCRIPTORS, "Test-Ingested-Logs", {
        "service": "AmazonCloudWatch",
        "attribute_filters": [{"key": "group", "value": "Ingested Logs"}],
        "unit": "GB",
    })
    monkeypatch.setattr("infra_cost_model.pricing.cache.PricingCache",
                        lambda *a, **k: MagicMock())
    with patch.object(ic.requests, "post", side_effect=_fake_post(CLOUDWATCH_LOGS_US_EAST_1)):
        with pytest.warns(UserWarning, match="Test-Ingested-Logs: .*matched 6 products"):
            ic.sync_pricing_catalog(services=["Test-Ingested-Logs", "CloudWatch-Log-Storage"])
