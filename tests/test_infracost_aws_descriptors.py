"""Descriptors for the AWS catalog metrics that a live sync used to skip (#470).

Each metric below had a seed row and a handler that names it, but no entry in
``METRIC_DESCRIPTORS``, so ``sync-pricing`` stored no live rows for it. The
fake Cloud Pricing API below holds the products that the live API returned on
2026-10-08 for us-east-1 and for the global catalogue (region ""), with the
sibling products that share a filter with each one. It answers a query the
way the live API does: it keeps the products of the region whose product
family and attributes equal the filter values, and it reads a missing
attribute as "".
"""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from infra_cost_model.pricing.catalog import PricingCatalog
from infra_cost_model.pricing.cache import load_seed_rows
from infra_cost_model.pricing.sources import infracost as ic

INF = "Inf"


def _product(region, family, attributes, prices):
    return {
        "region": region,
        "productFamily": family,
        "attributes": [{"key": k, "value": v} for k, v in attributes.items()],
        "prices": [
            {"USD": usd, "unit": unit, "startUsageAmount": start, "endUsageAmount": end}
            for usd, unit, start, end in prices
        ],
    }


def _use1(family, attributes, prices):
    return _product("us-east-1", family, attributes, prices)


def _global(family, attributes, prices):
    return _product("", family, attributes, prices)


CATALOGUE = [
    # API Gateway: the HTTP API product and the REST API product share a family.
    _use1("API Calls", {"usagetype": "USE1-ApiGatewayHttpRequest",
                        "operation": "ApiGatewayHttpApi"},
          [("0.000001", "Requests", "0", "300000000"),
           ("0.0000009", "Requests", "300000000", INF)]),
    _use1("API Calls", {"usagetype": "USE1-ApiGatewayRequest",
                        "operation": "ApiGatewayRequest"},
          [("0.0000035", "Requests", "0", "333000000")]),
    # ALB: every LCU dimension bills the one LCU-hour product.
    _use1("Load Balancer-Application", {"usagetype": "LCUUsage", "group": "ELB:Balancing"},
          [("0.008", "LCU-Hrs", "0", INF)]),
    _use1("Load Balancer-Application", {"usagetype": "Outposts-LCUUsage",
                                        "group": "ELB:Balancing"},
          [("0", "LCU-Hrs", "0", INF)]),
    _use1("Load Balancer-Application", {"usagetype": "ReservedLCUUsage",
                                        "group": "ELB:Balancing"},
          [("0.008", "ReservedLCU-Hr", "0", INF)]),
    # DynamoDB: standard-class storage and provisioned capacity, beside the
    # Standard-IA and PITR products. Each regional product starts with the
    # free tier of 25 GB and 25 RCU or WCU (18,600 unit-hours).
    _use1("Database Storage", {"usagetype": "TimedStorage-ByteHrs"},
          [("0", "GB-Mo", "0", "25"), ("0.25", "GB-Mo", "25", INF)]),
    _use1("Database Storage", {"usagetype": "USE1-TimedStorage-ByteHrs"},
          [("0.25", "GB-Mo", "0", INF)]),
    _use1("Database Storage", {"usagetype": "IA-TimedStorage-ByteHrs"},
          [("0.1", "GB-Mo", "0", INF)]),
    _use1("Database Storage", {"usagetype": "USE1-TimedPITRStorage-ByteHrs"},
          [("0.2", "GB-Mo", "0", INF)]),
    _use1("Provisioned IOPS", {"usagetype": "ReadCapacityUnit-Hrs", "group": "DDB-ReadUnits"},
          [("0", "ReadCapacityUnit-Hrs", "0", "18600"),
           ("0.00013", "ReadCapacityUnit-Hrs", "18600", INF)]),
    _use1("Provisioned IOPS", {"usagetype": "IA-ReadCapacityUnit-Hrs",
                               "group": "DDB-ReadUnitsIA"},
          [("0.00016", "ReadCapacityUnit-Hrs", "0", INF)]),
    _use1("Provisioned IOPS", {"usagetype": "WriteCapacityUnit-Hrs",
                               "group": "DDB-WriteUnits"},
          [("0", "WriteCapacityUnit-Hrs", "0", "18600"),
           ("0.00065", "WriteCapacityUnit-Hrs", "18600", INF)]),
    _use1("Provisioned IOPS", {"usagetype": "IA-WriteCapacityUnit-Hrs",
                               "group": "DDB-WriteUnitsIA"},
          [("0.00081", "WriteCapacityUnit-Hrs", "0", INF)]),
    # Fargate x86, beside the ARM products.
    _use1("Compute", {"usagetype": "USE1-Fargate-vCPU-Hours:perCPU"},
          [("0.04048", "hours", "0", INF)]),
    _use1("Compute", {"usagetype": "USE1-Fargate-GB-Hours"},
          [("0.004445", "hours", "0", INF)]),
    _use1("Compute", {"usagetype": "USE1-Fargate-ARM-vCPU-Hours:perCPU"},
          [("0.03238", "hours", "0", INF)]),
    # EventBridge: custom and partner events share the usagetype.
    _use1("EventBridge", {"usagetype": "USE1-Event-64K-Chunks", "operation": "PutEvents",
                          "eventType": "Custom Event"},
          [("0.000001", "64K-Chunks", "0", INF)]),
    _use1("EventBridge", {"usagetype": "USE1-Event-64K-Chunks",
                          "operation": "ReceivedPartnerEvents",
                          "eventType": "Partner Event"},
          [("0.000001", "64K-Chunks", "0", INF)]),
    # RDS for MySQL, Single-AZ, beside the Multi-AZ and other-engine products.
    _use1("Database Instance", {"usagetype": "InstanceUsage:db.t3.micro",
                                "instanceType": "db.t3.micro", "databaseEngine": "MySQL",
                                "deploymentOption": "Single-AZ"},
          [("0.017", "Hrs", "0", INF)]),
    _use1("Database Instance", {"usagetype": "Multi-AZUsage:db.t3.micro",
                                "instanceType": "db.t3.micro", "databaseEngine": "MySQL",
                                "deploymentOption": "Multi-AZ"},
          [("0.034", "Hrs", "0", INF)]),
    _use1("Database Instance", {"usagetype": "InstanceUsage:db.t3.micro",
                                "instanceType": "db.t3.micro",
                                "databaseEngine": "PostgreSQL",
                                "deploymentOption": "Single-AZ"},
          [("0.018", "Hrs", "0", INF)]),
    _use1("Database Instance", {"usagetype": "InstanceUsage:db.r6g.large",
                                "instanceType": "db.r6g.large", "databaseEngine": "MySQL",
                                "deploymentOption": "Single-AZ"},
          [("0.215", "Hrs", "0", INF)]),
    # RDS for PostgreSQL (#481), beside its Multi-AZ products.
    _use1("Database Instance", {"usagetype": "Multi-AZUsage:db.t3.micro",
                                "instanceType": "db.t3.micro",
                                "databaseEngine": "PostgreSQL",
                                "deploymentOption": "Multi-AZ"},
          [("0.036", "Hrs", "0", INF)]),
    _use1("Database Instance", {"usagetype": "InstanceUsage:db.r6g.large",
                                "instanceType": "db.r6g.large",
                                "databaseEngine": "PostgreSQL",
                                "deploymentOption": "Single-AZ"},
          [("0.225", "Hrs", "0", INF)]),
    _use1("Database Instance", {"usagetype": "Multi-AZUsage:db.r6g.large",
                                "instanceType": "db.r6g.large",
                                "databaseEngine": "PostgreSQL",
                                "deploymentOption": "Multi-AZ"},
          [("0.45", "Hrs", "0", INF)]),
    _use1("Database Storage", {"usagetype": "USE1-RDS:GP3-Storage", "databaseEngine": "MySQL",
                               "deploymentOption": "Single-AZ"},
          [("0.115", "GB-Mo", "0", INF)]),
    _use1("Database Storage", {"usagetype": "USE1-RDS:Multi-AZ-GP3-Storage",
                               "databaseEngine": "MySQL", "deploymentOption": "Multi-AZ"},
          [("0.23", "GB-Mo", "0", INF)]),
    _use1("Database Storage", {"usagetype": "USE1-RDS:GP3-Storage",
                               "databaseEngine": "PostgreSQL",
                               "deploymentOption": "Single-AZ"},
          [("0.115", "GB-Mo", "0", INF)]),
    _use1("Storage Snapshot", {"usagetype": "RDS:ChargedBackupUsage",
                               "databaseEngine": "MySQL", "engineCode": "2"},
          [("0.095", "GB-Mo", "0", INF)]),
    _use1("Storage Snapshot", {"usagetype": "RDSCustom:ChargedBackupUsage",
                               "databaseEngine": "MySQL"},
          [("0.095", "GB-Mo", "0", INF)]),
    _use1("Storage Snapshot", {"usagetype": "RDS:ChargedBackupUsage",
                               "databaseEngine": "Oracle", "engineCode": "20"},
          [("0.095", "GB-Mo", "0", INF)]),
    # S3: GET requests are Tier 2; Standard storage shares its usagetype with
    # the Intelligent-Tiering overhead products.
    _use1("API Request", {"usagetype": "Requests-Tier2", "group": "S3-API-Tier2"},
          [("0.0000004", "Requests", "0", INF)]),
    _use1("API Request", {"usagetype": "Requests-Tier1", "group": "S3-API-Tier1"},
          [("0.000005", "Requests", "0", INF)]),
    _use1("Storage", {"usagetype": "TimedStorage-ByteHrs", "volumeType": "Standard"},
          [("0.023", "GB-Mo", "0", "51200"), ("0.022", "GB-Mo", "51200", "512000"),
           ("0.021", "GB-Mo", "512000", INF)]),
    _use1("Storage", {"usagetype": "TimedStorage-ByteHrs",
                      "volumeType": "INTAAS3ObjectOverhead"},
          [("0.021", "GB-Mo", "0", INF)]),
    # SNS: us-east-1 states the free tiers in the products themselves.
    _use1("API Request", {"usagetype": "Requests-Tier1", "group": "SNS-Requests-Tier1"},
          [("0", "Requests", "0", "1000000"), ("0.0000005", "Requests", "1000000", INF)]),
    _use1("Message Delivery", {"usagetype": "DeliveryAttempts-HTTP", "endpointType": "HTTP"},
          [("0", "Notifications", "0", "100000"),
           ("0.0000006", "Notifications", "100000", INF)]),
    _use1("Message Delivery", {"usagetype": "DeliveryAttempts-SQS",
                               "endpointType": "Amazon SQS"},
          [("0", "Notifications", "0", INF)]),
    _use1("Message Delivery", {"usagetype": "DeliveryAttempts-LAMBDA",
                               "endpointType": "AWS Lambda"},
          [("0", "Notifications", "0", INF)]),
    _use1("Message Delivery", {"usagetype": "DeliveryAttempts-SMTP", "endpointType": "SMTP"},
          [("0", "Notifications", "0", "1000"), ("0.00002", "Notifications", "1000", INF)]),
    # SQS: us-east-1 names the request products "Requests-RBP", other regions
    # "EU-Requests-Tier1", so the queue type selects them.
    _use1("API Request", {"usagetype": "Requests-RBP", "group": "SQS-APIRequest-Tier1",
                          "queueType": "Standard"},
          [("0.0000004", "Requests", "0", "100000000000"),
           ("0.0000003", "Requests", "100000000000", "200000000000"),
           ("0.00000024", "Requests", "200000000000", INF)]),
    _use1("API Request", {"usagetype": "Requests-FIFO-RBP", "group": "SQS-APIRequest-Tier1",
                          "queueType": "FIFO (first-in, first-out)"},
          [("0.0000005", "Requests", "0", "100000000000"),
           ("0.0000004", "Requests", "100000000000", "200000000000"),
           ("0.00000035", "Requests", "200000000000", INF)]),
    _use1("API Request", {"usagetype": "Requests-Fair-RBP", "group": "SQS-APIRequest-Tier1",
                          "queueType": "Fair"},
          [("0.0000001", "Requests", "0", INF)]),
    # CloudFront: the global catalogue names the edge location in the usagetype.
    _global("Data Transfer", {"usagetype": "US-DataTransfer-Out-Bytes"},
            [("0.085", "GB", "0", "10240"), ("0.08", "GB", "10240", "51200"),
             ("0.06", "GB", "51200", INF)]),
    _global("Data Transfer", {"usagetype": "US-DataTransfer-Out-OBytes"},
            [("0.02", "GB", "0", INF)]),
    _global("Data Transfer", {"usagetype": "ME-DataTransfer-Out-Bytes"},
            [("0.11", "GB", "0", "10240")]),
    _global("Request", {"usagetype": "US-Requests-Tier2-HTTPS"},
            [("0.000001", "Requests", "0", INF)]),
    _global("Request", {"usagetype": "US-Requests-Tier1"},
            [("0.00000075", "Requests", "0", INF)]),
    _global("Request", {"usagetype": "US-Requests-HTTPS-Proxy"},
            [("0.000001", "Requests", "0", INF)]),
    _global("Request", {"usagetype": "Global-Requests-Tier1"},
            [("0", "Requests", "0", "10000000")]),
]


def _fake_post(catalogue=CATALOGUE):
    """Return a `requests.post` stand-in that filters *catalogue* like the API."""
    def post(url, headers=None, json=None, timeout=None):
        variables = json["variables"]
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
                matched.append({k: v for k, v in product.items() if k != "region"})
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


def _sync(catalog, metric, region):
    with patch.object(ic.requests, "post", side_effect=_fake_post()):
        ic.InfracostClient().sync_to_cache(catalog._cache, metric, region)


def _live_rows(catalog, metric, region):
    conn = sqlite3.connect(catalog._cache.db_path)
    try:
        return conn.execute(
            "SELECT service, price_usd, start_usage_amount FROM prices "
            "WHERE usage_metric = ? AND region = ? AND source = 'infracost' "
            "ORDER BY start_usage_amount", (metric, region)).fetchall()
    finally:
        conn.close()


# metric: (service the handler queries, sync region, the (price, tier start)
# rows the sync stores).
EXPECTED = {
    "APIGateway-HTTP-Request": ("AmazonAPIGatewayHTTP", "us-east-1",
                                [(0.000001, 0), (0.0000009, 300_000_000)]),
    "ALB-LCU-ActiveConnections": ("AmazonALB", "us-east-1", [(0.008, 0)]),
    "ALB-LCU-NewConnections": ("AmazonALB", "us-east-1", [(0.008, 0)]),
    "ALB-LCU-RuleEvaluations": ("AmazonALB", "us-east-1", [(0.008, 0)]),
    "CloudFront-DataTransfer": ("AmazonCloudFront", "global",
                                [(0, 0), (0.085, 1024), (0.08, 10_240), (0.06, 51_200)]),
    "CloudFront-HTTPS-Request": ("AmazonCloudFront", "global",
                                 [(0, 0), (0.000001, 10_000_000)]),
    "CloudFront-HTTP-Request": ("AmazonCloudFront", "global",
                                [(0, 0), (0.00000075, 10_000_000)]),
    "Dynamo-Storage": ("AmazonDynamoDB", "us-east-1", [(0, 0), (0.25, 25)]),
    "Dynamo-RCU-Hour": ("AmazonDynamoDB", "us-east-1", [(0, 0), (0.00013, 18_600)]),
    "Dynamo-WCU-Hour": ("AmazonDynamoDB", "us-east-1", [(0, 0), (0.00065, 18_600)]),
    "ECS-Fargate-vCPU-Hour": ("AmazonECS", "us-east-1", [(0.04048, 0)]),
    "ECS-Fargate-GB-Hour": ("AmazonECS", "us-east-1", [(0.004445, 0)]),
    "EventBridge-CustomEvent": ("AmazonEventBridge", "us-east-1", [(0.000001, 0)]),
    "RDS-Instance-Hour-db.t3.micro": ("AmazonRDS", "us-east-1", [(0.017, 0)]),
    "RDS-Instance-Hour-postgres-db.t3.micro": ("AmazonRDS", "us-east-1", [(0.018, 0)]),
    "RDS-Storage-gp3": ("AmazonRDS", "us-east-1", [(0.115, 0)]),
    "RDS-Backup-Storage": ("AmazonRDS", "us-east-1", [(0.095, 0)]),
    "S3-GetRequest": ("AmazonS3", "us-east-1", [(0.0000004, 0)]),
    "S3-Storage": ("AmazonS3", "us-east-1",
                   [(0.023, 0), (0.022, 51_200), (0.021, 512_000)]),
    "SNS-Publish": ("AmazonSNS", "us-east-1", [(0, 0), (0.0000005, 1_000_000)]),
    "SNS-Delivery-HTTP": ("AmazonSNS", "us-east-1", [(0, 0), (0.0000006, 100_000)]),
    "SNS-Delivery-SQS": ("AmazonSNS", "us-east-1", [(0, 0)]),
    "SNS-Delivery-Lambda": ("AmazonSNS", "us-east-1", [(0, 0)]),
    "SQS-Standard-Request": ("AmazonSQS", "us-east-1",
                             [(0, 0), (0.0000004, 1_000_000),
                              (0.0000003, 100_000_000_000),
                              (0.00000024, 200_000_000_000)]),
    "SQS-FIFO-Request": ("AmazonSQS", "us-east-1",
                         [(0, 0), (0.0000005, 1_000_000),
                          (0.0000004, 100_000_000_000),
                          (0.00000035, 200_000_000_000)]),
}


@pytest.mark.parametrize("metric", sorted(EXPECTED))
def test_sync_stores_the_one_product_of_the_metric(creds, tmp_path, metric):
    service, region, tiers = EXPECTED[metric]
    catalog = PricingCatalog(db_path=tmp_path / "pricing.db")
    _sync(catalog, metric, region)
    rows = _live_rows(catalog, metric, region)
    assert {s for s, _, _ in rows} == {service}
    assert [(p, s) for _, p, s in rows] == [pytest.approx(t) for t in tiers]


# Quantities below the first tier bound where the seed rows stop: the seed
# file states fewer paid tiers than AWS for some of these metrics.
QUANTITIES = {
    "APIGateway-HTTP-Request": [0, 1_000_000, 250_000_000],
    "ALB-LCU-ActiveConnections": [0, 730],
    "ALB-LCU-NewConnections": [0, 730],
    "ALB-LCU-RuleEvaluations": [0, 730],
    "CloudFront-DataTransfer": [0, 500, 1024, 5_000, 20_000],
    "CloudFront-HTTPS-Request": [0, 5_000_000, 50_000_000],
    "CloudFront-HTTP-Request": [0, 5_000_000, 50_000_000],
    "ECS-Fargate-vCPU-Hour": [0, 730],
    "ECS-Fargate-GB-Hour": [0, 1460],
    "EventBridge-CustomEvent": [0, 3_000_000],
    "RDS-Instance-Hour-db.t3.micro": [0, 730],
    "RDS-Instance-Hour-postgres-db.t3.micro": [0, 730],
    "RDS-Storage-gp3": [0, 100],
    "RDS-Backup-Storage": [0, 100],
    "S3-GetRequest": [0, 1_000_000],
    "S3-Storage": [0, 100, 60_000, 500_000],
    "SNS-Publish": [0, 500_000, 3_000_000],
    "SNS-Delivery-HTTP": [0, 50_000, 1_000_000],
    "SNS-Delivery-SQS": [0, 1_000_000],
    "SNS-Delivery-Lambda": [0, 1_000_000],
    "SQS-Standard-Request": [0, 500_000, 5_000_000],
    "SQS-FIFO-Request": [0, 500_000, 5_000_000],
}


@pytest.mark.parametrize("metric", sorted(QUANTITIES))
def test_live_sync_prices_usage_as_the_seed_does(creds, tmp_path, metric):
    service, region, _ = EXPECTED[metric]
    seed = PricingCatalog(db_path=tmp_path / "seed.db", seed=True)
    live = PricingCatalog(db_path=tmp_path / "live.db", seed=True)
    _sync(live, metric, region)
    for quantity in QUANTITIES[metric]:
        want = seed.query("aws", service, region, metric, quantity)
        got = live.query("aws", service, region, metric, quantity)
        assert got.total_cost == pytest.approx(want.total_cost), quantity


@pytest.mark.parametrize("metric,allowance", [("Dynamo-Storage", 25),
                                              ("Dynamo-RCU-Hour", 18_600),
                                              ("Dynamo-WCU-Hour", 18_600)])
def test_dynamodb_live_rows_keep_the_free_tier_of_each_region(creds, tmp_path, metric,
                                                             allowance):
    """AWS gives 25 GB, 25 RCU and 25 WCU free "on a per Region, per-payer
    account basis" (https://aws.amazon.com/dynamodb/pricing/provisioned/,
    checked 2026-10-08). The seed rows state no free tier, so below the
    allowance a live catalog charges $0 where the seed one charges the rate.
    """
    service, region, tiers = EXPECTED[metric]
    live = PricingCatalog(db_path=tmp_path / "live.db")
    _sync(live, metric, region)
    assert live.query("aws", service, region, metric, allowance).total_cost == 0
    above = live.query("aws", service, region, metric, allowance + 100).total_cost
    assert above == pytest.approx(100 * tiers[-1][0])


def test_every_seed_rds_instance_class_has_a_descriptor():
    """A default sync prices each instance class that the seed file prices."""
    seed = {r.usage_metric for r in load_seed_rows()
            if r.usage_metric.startswith(ic.RDS_INSTANCE_HOUR_PREFIX)}
    assert seed
    assert seed <= set(ic.METRIC_DESCRIPTORS)


def test_any_rds_instance_class_resolves_to_a_descriptor(creds, tmp_path):
    """A class outside the seed file still syncs when named, e.g. on the CLI."""
    metric = "RDS-Instance-Hour-db.r6g.large"
    assert metric not in ic.METRIC_DESCRIPTORS
    query = ic.parse_descriptor(metric, "us-east-1")
    assert {"key": "instanceType", "value": "db.r6g.large"} in query.attribute_filters
    catalog = PricingCatalog(db_path=tmp_path / "pricing.db")
    _sync(catalog, metric, "us-east-1")
    assert _live_rows(catalog, metric, "us-east-1") == [("AmazonRDS", 0.215, 0.0)]


def test_any_postgres_instance_class_resolves_to_a_postgres_descriptor(creds, tmp_path):
    """The PostgreSQL rows (#481) name the engine before the class."""
    metric = "RDS-Instance-Hour-postgres-db.r6g.large"
    assert metric not in ic.METRIC_DESCRIPTORS
    query = ic.parse_descriptor(metric, "us-east-1")
    assert {"key": "instanceType", "value": "db.r6g.large"} in query.attribute_filters
    assert {"key": "databaseEngine", "value": "PostgreSQL"} in query.attribute_filters
    catalog = PricingCatalog(db_path=tmp_path / "pricing.db")
    _sync(catalog, metric, "us-east-1")
    assert _live_rows(catalog, metric, "us-east-1") == [("AmazonRDS", 0.225, 0.0)]


@pytest.mark.parametrize("metric", ["RDS-Instance-Hour-postgres-", "RDS-Instance-Hour-"])
def test_an_rds_metric_without_a_class_has_no_descriptor(metric):
    assert ic.descriptor_for(metric) is None


def test_a_named_rds_class_syncs_through_sync_pricing_catalog(creds, tmp_path,
                                                              monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with patch.object(ic.requests, "post", side_effect=_fake_post()):
        count, source = ic.sync_pricing_catalog(
            services=["RDS-Instance-Hour-db.r6g.large"], regions=["us-east-1"])
    assert (count, source) == (1, "infracost")


@pytest.mark.parametrize("metric", ["CloudFront-DataTransfer", "CloudFront-HTTPS-Request",
                                    "CloudFront-HTTP-Request"])
def test_cloudfront_is_priced_under_global_only(metric):
    """CloudFront has no AWS region: a regional sync of it would store rows
    that no handler queries, so the descriptor refuses one."""
    assert ic.parse_descriptor(metric, ic.GLOBAL_REGION).query_region == ""
    with pytest.raises(KeyError, match="global"):
        ic.parse_descriptor(metric, "us-east-1")


def test_a_regional_sync_skips_the_global_only_metrics(creds, tmp_path, monkeypatch):
    """The us-east-1 pass sends no query; the global pass stores the rows."""
    monkeypatch.setenv("HOME", str(tmp_path))
    regions = []

    def post(url, headers=None, json=None, timeout=None):
        regions.append(json["variables"]["region"])
        return _fake_post()(url, headers, json, timeout)

    with patch.object(ic.requests, "post", side_effect=post):
        count, source = ic.sync_pricing_catalog(
            services=["CloudFront-DataTransfer"], regions=["us-east-1", ic.GLOBAL_REGION])
    assert regions == [""]
    assert (count, source) == (4, "infracost")
