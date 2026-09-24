"""Infracost descriptors for ALB, ECR storage and S3 PUT select one product (#352-#354).

The fake Cloud Pricing API below holds the products that the live API and the
AWS price lists (``AWSELB``, ``AmazonECR``, ``AmazonS3``, checked 2026-09-24)
return for us-east-1 and ca-central-1. It answers a query the way the live API
does: it keeps the products whose product family and attributes equal the
filter values, and it reads a missing attribute as "". The tests sync through
that fake API and check which products reach the cache.
"""

from unittest.mock import MagicMock, patch

import pytest

from infra_cost_model.pricing.cache import Price, PricingCache, load_seed_rows
from infra_cost_model.pricing.sources import infracost as ic


def _product(family, usagetype, prices, **attrs):
    attributes = [{"key": "usagetype", "value": usagetype}]
    attributes += [{"key": k, "value": v} for k, v in attrs.items()]
    return {
        "productFamily": family,
        "attributes": attributes,
        "prices": [
            {"USD": usd, "unit": unit, "startUsageAmount": start, "endUsageAmount": end}
            for usd, unit, start, end in prices
        ],
    }


def _alb(usagetype, usd, unit):
    return _product("Load Balancer-Application", usagetype, [(usd, unit, "0", None)],
                    group="ELB:Balancing")


def _ecr(usagetype, prices):
    return _product("EC2 Container Registry", usagetype, prices)


def _s3(usagetype, usd, group, family="API Request"):
    return _product(family, usagetype, [(usd, "Requests", "0", None)], group=group)


def _catalogue(prefix):
    """The products of one region; *prefix* is "" in us-east-1 and "CAN1-" in ca-central-1."""
    archive = "USE1-" if prefix == "" else prefix
    return [
        _alb(f"{prefix}LoadBalancerUsage", "0.0225", "Hrs"),
        _alb(f"{prefix}Outposts-LoadBalancerUsage", "0.0225", "Hrs"),
        _alb(f"{prefix}TS-LoadBalancerUsage", "0.005", "Hrs"),
        _alb(f"{prefix}LCUUsage", "0.008", "LCU-Hrs"),
        _alb(f"{prefix}Outposts-LCUUsage", "0", "LCU-Hrs"),
        _alb(f"{prefix}ReservedLCUUsage", "0.008", "ReservedLCU-Hr"),
        _ecr(f"{prefix}TimedStorage-ByteHrs", [("0.1", "GB-Mo", "0", None)]),
        _ecr(f"{archive}TimedStorage-Archive-ByteHrs",
             [("0.1", "GB-Mo", "0", "153600"), ("0.07", "GB-Mo", "153600", None)]),
        _ecr(f"{archive}Retrieval-Archive", [("0.03", "GB", "0", None)]),
        _s3(f"{prefix}Requests-Tier1", "0.000005", "S3-API-Tier1"),
        _s3(f"{prefix}Requests-Tier2", "0.0000004", "S3-API-Tier2"),
        _s3(f"{prefix}Tables-Requests-Tier1", "0.000005", "S3-API-Tables-Tier1", family=""),
    ]


US_EAST_1 = _catalogue("")
CA_CENTRAL_1 = _catalogue("CAN1-")


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


def _sync(metric, catalogue=US_EAST_1, region="us-east-1"):
    upserted = []
    cache = MagicMock()
    cache.upsert.side_effect = upserted.append
    with patch.object(ic.requests, "post", side_effect=_fake_post(catalogue)):
        n = ic.InfracostClient().sync_to_cache(cache, metric, region)
    assert n == len(upserted)
    return upserted


def _seed_row(service, metric):
    rows = [r for r in load_seed_rows([service])
            if r.usage_metric == metric and r.region == "us-east-1"]
    assert len(rows) == 1
    return rows[0]


# Each case: metric, the service the handler queries, the usagetype the sync
# must keep, and the unit of the AWS product. The seed spells some units
# differently ("Hours" for "Hrs"), so the unit is checked against AWS.
CASES = [
    ("ALB-Hour", "AmazonALB", "LoadBalancerUsage", "Hrs"),
    ("ALB-LCU-ProcessedBytes", "AmazonALB", "LCUUsage", "LCU-Hrs"),
    ("ECR-Storage", "AmazonECR", "TimedStorage-ByteHrs", "GB-Mo"),
    ("S3-PutRequest", "AmazonS3", "Requests-Tier1", "Requests"),
]


@pytest.mark.parametrize("metric,service,usagetype,unit", CASES)
def test_sync_stores_one_product_at_the_seed_price(creds, metric, service, usagetype, unit):
    rows = _sync(metric)
    assert [r.attributes["usagetype"] for r in rows] == [usagetype]
    seed = _seed_row(service, metric)
    assert rows[0].service == seed.service == service
    assert rows[0].price_usd == pytest.approx(seed.price_usd)
    assert rows[0].unit == unit


@pytest.mark.parametrize("metric,service,usagetype,unit", CASES)
def test_sync_selects_the_prefixed_usagetype_in_ca_central_1(creds, metric, service, usagetype, unit):
    rows = _sync(metric, catalogue=CA_CENTRAL_1, region="ca-central-1")
    assert [r.attributes["usagetype"] for r in rows] == [f"CAN1-{usagetype}"]
    assert rows[0].service == service
    assert rows[0].unit == unit


@pytest.mark.parametrize("region,prefix", [("us-east-1", ""), ("ca-central-1", "CAN1-"),
                                           ("eu-west-1", "EU-")])
def test_descriptors_query_the_region_usagetype(creds, region, prefix):
    captured = []

    def post(url, headers=None, json=None, timeout=None):
        captured.append(json["variables"]["attributeFilters"])
        return _fake_post([])(url, headers, json, timeout)

    with patch.object(ic.requests, "post", side_effect=post):
        for metric, _, _, _ in CASES:
            ic.InfracostClient().sync_to_cache(MagicMock(), metric, region)
    assert captured == [[{"key": "usagetype", "value": f"{prefix}{u}"}] for _, _, u, _ in CASES]


def test_synced_prices_reach_the_handler_queries(creds, tmp_path):
    """The handlers query AmazonALB, AmazonECR and AmazonS3 and find the live rows."""
    cache = PricingCache(db_path=tmp_path / "pricing.db")
    client = ic.InfracostClient()
    with patch.object(ic.requests, "post", side_effect=_fake_post(US_EAST_1)):
        for metric, _, _, _ in CASES:
            client.sync_to_cache(cache, metric, "us-east-1")
    # 730 ALB-hours, 730 LCU-hours, 10 GB-months, 1,000 PUT requests.
    for metric, service, quantity, expected in (
        ("ALB-Hour", "AmazonALB", 730, 16.425),
        ("ALB-LCU-ProcessedBytes", "AmazonALB", 730, 5.84),
        ("ECR-Storage", "AmazonECR", 10, 1.00),
        ("S3-PutRequest", "AmazonS3", 1000, 0.005),
    ):
        price = cache.query("aws", service, "us-east-1", metric)
        assert isinstance(price, Price)
        assert price.source == "infracost"
        assert price.price_usd * quantity == pytest.approx(expected)


def test_unprefixed_flag_without_filters_queries_the_family(creds, monkeypatch):
    """The us-east-1 flag leaves a descriptor that has no attribute filters unchanged."""
    monkeypatch.setitem(ic.METRIC_DESCRIPTORS, "Test-ECR-Family", {
        "service": "AmazonECR", "product_family": "EC2 Container Registry",
        "unprefixed_in_us_east_1": True, "unit": "GB",
    })
    rows = _sync("Test-ECR-Family")
    assert [r.attributes["usagetype"] for r in rows] == ["USE1-Retrieval-Archive"]
