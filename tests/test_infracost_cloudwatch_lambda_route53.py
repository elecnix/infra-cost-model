"""Infracost descriptors for CloudWatch, Lambda and Route 53 in every region.

#359: the CloudWatch metric, alarm and GetMetricData descriptors named the
bare us-east-1 usagetypes, so they matched no product in other regions.

#360: the Lambda descriptors kept one unit spelling, and each region spells
the unit its own way, so they matched no product in ca-central-1.

#361: the Route 53 hosted-zone descriptor selected the DNS Firewall domain
name product. The hosted-zone product is in the global catalog.

#367: the seed Lambda-GB-Second rows lacked the volume tiers above 6 billion
GB-seconds.

The fake Cloud Pricing API below holds the products that the live API
returned for us-east-1, ca-central-1 and eu-west-1 on 2026-09-24. The AWS
price lists for AWSLambda, AmazonCloudWatch and AmazonRoute53 give the same
usagetypes and prices. The fake API answers a query the way the live API
does: it keeps the products of the queried region whose product family and
attributes equal the filter values.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from infra_cost_model.pricing.cache import SEED_PRICES_PATH, load_seed_rows
from infra_cost_model.pricing.catalog import PricingCatalog
from infra_cost_model.pricing.sources import infracost as ic

INF = "Inf"
GB_SECOND_TIERS = [("0.0000166667", "0", "6000000000"),
                   ("0.000015", "6000000000", "15000000000"),
                   ("0.0000133334", "15000000000", INF)]
METRIC_TIERS = [("0.3", "0", "10000"), ("0.1", "10000", "250000"),
                ("0.05", "250000", "1000000"), ("0.02", "1000000", INF)]


def _product(region, family, usagetype, prices, **attrs):
    attributes = {"usagetype": usagetype, **attrs}
    return {
        "region": region,
        "productFamily": family,
        "attributes": [{"key": k, "value": v} for k, v in attributes.items()],
        "prices": [
            {"USD": usd, "unit": unit, "startUsageAmount": start, "endUsageAmount": end}
            for usd, unit, start, end in prices
        ],
    }


def _tiers(tiers, unit):
    return [(usd, unit, start, end) for usd, start, end in tiers]


def _regional(region, prefix, request_units, gb_second_units):
    """The products of one region. *prefix* is "" in us-east-1."""
    return [
        _product(region, "Serverless", f"{prefix}Request",
                 [("0.0000002", unit, "0", INF) for unit in request_units],
                 group="AWS-Lambda-Requests"),
        _product(region, "Serverless", f"{prefix}Lambda-GB-Second",
                 [p for unit in gb_second_units for p in _tiers(GB_SECOND_TIERS, unit)],
                 group="AWS-Lambda-Duration"),
        _product(region, "Serverless", f"{prefix}Lambda-GB-Second-ARM",
                 [("0.0000133334", "Lambda-GB-Second", "0", INF)],
                 group="AWS-Lambda-Duration-ARM"),
        _product(region, "Metric", f"{prefix}CW:MetricMonitorUsage",
                 _tiers(METRIC_TIERS, "Metrics")),
        _product(region, "Alarm", f"{prefix}CW:AlarmMonitorUsage",
                 [("0.1", "Alarms", "0", INF)]),
        _product(region, "Alarm", f"{prefix}CW:HighResAlarmMonitorUsage",
                 [("0.3", "Alarms", "0", INF)]),
        _product(region, "API Request", f"{prefix}CW:GMD-Metrics",
                 [("0.00001", "Metrics", "0", INF)]),
        _product(region, "DNS Domain Names",
                 f"{prefix or 'USE1-'}DNS-FirewallDomainName",
                 [("0.0005", "Mo", "0", INF)]),
    ]


GLOBAL = [
    _product("", "DNS Zone", "HostedZone",
             [("0.5", "HostedZone", "0", "25"), ("0.1", "HostedZone", "25", INF)]),
    _product("", "DNS Zone", "Global-RRSets", [("0.0015", "Mo", "0", INF)]),
]

CATALOGUE = (
    _regional("us-east-1", "", ["Requests"], ["seconds", "Lambda-GB-Second"])
    + _regional("ca-central-1", "CAN1-", ["Request"], ["Lambda-GB-Second"])
    + _regional("eu-west-1", "EU-", ["Requests", "Request"],
                ["Second", "Lambda-GB-Second"])
    + GLOBAL
)

REGIONS = {"us-east-1": "", "ca-central-1": "CAN1-", "eu-west-1": "EU-"}


def _fake_post(catalogue=CATALOGUE, captured=None):
    """Return a `requests.post` stand-in that filters *catalogue* like the API."""
    def post(url, headers=None, json=None, timeout=None):
        variables = json["variables"]
        if captured is not None:
            captured.append(variables)
        family = variables.get("productFamily")
        filters = variables.get("attributeFilters") or []
        matched = []
        for product in catalogue:
            attrs = {a["key"]: a["value"] for a in product["attributes"]}
            if product["region"] != variables["region"]:
                continue
            if family and product["productFamily"] != family:
                continue
            if all(attrs.get(f["key"], "") == f["value"] for f in filters):
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


def _sync(metric, region, catalogue=CATALOGUE):
    upserted = []
    cache = MagicMock()
    cache.upsert.side_effect = upserted.append
    with patch.object(ic.requests, "post", side_effect=_fake_post(catalogue)):
        n = ic.InfracostClient().sync_to_cache(cache, metric, region)
    assert n == len(upserted)
    return upserted


def _tiers_of(rows):
    return [(r.price_usd, r.start_usage_amount, r.end_usage_amount) for r in rows]


def _seed(metric):
    return [r for r in load_seed_rows()
            if r.usage_metric == metric and r.region == "us-east-1"]


# --- #359: CloudWatch in every region ------------------------------------------

CLOUDWATCH = [
    ("CloudWatch-Metric-Month", "CW:MetricMonitorUsage", "Metrics",
     [(0.0, 0.0, 10.0), (0.3, 10.0, 10000.0), (0.1, 10000.0, 250000.0),
      (0.05, 250000.0, 1000000.0), (0.02, 1000000.0, None)]),
    ("CloudWatch-Alarm-Month", "CW:AlarmMonitorUsage", "Alarms",
     [(0.0, 0.0, 10.0), (0.1, 10.0, None)]),
    ("CloudWatch-GetMetricData", "CW:GMD-Metrics", "Metrics",
     [(0.00001, 0.0, None)]),
]


@pytest.mark.parametrize("region,prefix", sorted(REGIONS.items()))
@pytest.mark.parametrize("metric,usagetype,unit,tiers", CLOUDWATCH)
def test_cloudwatch_selects_the_region_product(creds, region, prefix, metric,
                                               usagetype, unit, tiers):
    rows = _sync(metric, region)
    assert {r.attributes["usagetype"] for r in rows} == {f"{prefix}{usagetype}"}
    assert {r.region for r in rows} == {region}
    assert {r.unit for r in rows} == {unit}
    # The free tier of #356 comes first, then the paid tiers.
    got = [(p, s, None if e in (None, float("inf")) else e)
           for p, s, e in _tiers_of(rows)]
    assert got == tiers


# --- #360: Lambda in every region -----------------------------------------------

@pytest.mark.parametrize("region,prefix", sorted(REGIONS.items()))
def test_lambda_requests_store_one_unit_spelling(creds, region, prefix):
    rows = _sync("Lambda-Request", region)
    assert [r.attributes["usagetype"] for r in rows] == [f"{prefix}Request"] * 2
    assert _tiers_of(rows) == [(0.0, 0.0, 1_000_000.0),
                               (0.0000002, 1_000_000.0, float("inf"))]
    assert {r.unit for r in rows} == {"requests"}


@pytest.mark.parametrize("region,prefix", sorted(REGIONS.items()))
def test_lambda_gb_seconds_store_one_unit_spelling(creds, region, prefix):
    rows = _sync("Lambda-GB-Second", region)
    assert {r.attributes["usagetype"] for r in rows} == {f"{prefix}Lambda-GB-Second"}
    assert _tiers_of(rows) == [
        (0.0, 0.0, 400_000.0),
        (0.0000166667, 400_000.0, 6e9),
        (0.000015, 6e9, 15e9),
        (0.0000133334, 15e9, float("inf")),
    ]
    assert {r.unit for r in rows} == {"GB-s"}


def test_unit_list_prefers_the_first_spelling_present():
    prices = [{"unit": u, "price_usd": 1.0, "attributes": {}} for u in ("b", "c", "b")]
    assert [p["unit"] for p in ic._with_unit(prices, ["a", "b", "c"])] == ["b", "b"]
    assert [p["unit"] for p in ic._with_unit(prices, "c")] == ["c"]
    assert ic._with_unit(prices, None) == prices


# --- #361: Route 53 hosted zones --------------------------------------------------

@pytest.mark.parametrize("region", sorted(REGIONS))
def test_route53_queries_the_global_hosted_zone_product(creds, region):
    captured = []
    upserted = []
    cache = MagicMock()
    cache.upsert.side_effect = upserted.append
    with patch.object(ic.requests, "post", side_effect=_fake_post(captured=captured)):
        ic.InfracostClient().sync_to_cache(cache, "Route53-HostedZone", region)
    assert [v["region"] for v in captured] == [""]
    assert {r.attributes["usagetype"] for r in upserted} == {"HostedZone"}
    assert {r.region for r in upserted} == {region}
    assert {r.unit for r in upserted} == {"Zones"}
    assert _tiers_of(upserted) == [(0.5, 0.0, 25.0), (0.1, 25.0, float("inf"))]


# --- #367: the seed rows match the live rows --------------------------------------

@pytest.mark.parametrize("metric", ["Lambda-Request", "Lambda-GB-Second",
                                    "Route53-HostedZone"])
def test_seed_rows_match_the_live_rows(creds, metric):
    live = _sync(metric, "us-east-1")
    seed = _seed(metric)

    def norm(rows):
        return sorted((r.unit, r.price_usd, r.start_usage_amount or 0.0,
                       None if r.end_usage_amount in (None, float("inf"))
                       else r.end_usage_amount) for r in rows)
    assert norm(live) == norm(seed)


def test_new_seed_tiers_state_their_source():
    rows = json.loads(SEED_PRICES_PATH.read_text())
    volume = [r for r in rows if r["usage_metric"] == "Lambda-GB-Second"
              and r.get("start_usage_amount", 0) >= 6e9]
    assert len(volume) == 2
    for r in volume:
        assert r["effective_date"] == "2026-09-24"
        assert r["source"] == "https://aws.amazon.com/lambda/pricing/"


@pytest.mark.parametrize("service,metric,quantities", [
    ("AWSLambda", "Lambda-GB-Second",
     [0, 400_000, 6_000_000_000, 10_000_000_000, 20_000_000_000]),
    ("AmazonRoute53", "Route53-HostedZone", [1, 25, 30, 100]),
])
def test_live_and_seed_catalogs_agree(creds, tmp_path, service, metric, quantities):
    seed = PricingCatalog(db_path=tmp_path / "seed.db", seed=True)
    live = PricingCatalog(db_path=tmp_path / "live.db", seed=True)
    with patch.object(ic.requests, "post", side_effect=_fake_post()):
        ic.InfracostClient().sync_to_cache(live._cache, metric, "us-east-1")
    for q in quantities:
        want = seed.query("aws", service, "us-east-1", metric, q).total_cost
        got = live.query("aws", service, "us-east-1", metric, q).total_cost
        assert got == pytest.approx(want), q


def test_20_billion_gb_seconds_use_the_volume_tiers(seed_catalog):
    # 400K free, then 5.9996B at $0.0000166667, 9B at $0.000015, 5B at $0.0000133334.
    want = (6e9 - 400_000) * 0.0000166667 + 9e9 * 0.000015 + 5e9 * 0.0000133334
    got = seed_catalog.query("aws", "AWSLambda", "us-east-1", "Lambda-GB-Second",
                             20e9).total_cost
    assert got == pytest.approx(want)


def test_30_hosted_zones_cost_25_at_50_cents_and_5_at_10_cents(seed_catalog):
    got = seed_catalog.query("aws", "AmazonRoute53", "us-east-1",
                             "Route53-HostedZone", 30).total_cost
    assert got == pytest.approx(25 * 0.5 + 5 * 0.1)
