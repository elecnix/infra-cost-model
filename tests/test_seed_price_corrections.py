"""Seed rows match AWS's published us-east-1 on-demand prices (#311).

Later fixes: SQS has no storage charge (#324), CloudFront has no charge per
origin fetch (#325), scheduled rules cost nothing (#326), the first 100 GB
of data transfer out each month are free (#327), S3 egress shares those
data transfer rows (#332), and CloudFront has an always-free tier (#333).

Each case prices a quantity from the seed catalog and compares it with the
price on the AWS page that the row's ``source`` field names.
"""

import inspect
import json
import warnings

import pytest

from infra_cost_model.engine import CostEngine
from infra_cost_model.engine.engine import UnpricedMetricWarning
from infra_cost_model.pricing.cache import SEED_PRICES_PATH
from infra_cost_model.resources.apigw import _apigw_egress_cost
from infra_cost_model.resources.cloudfront import CloudFrontDistribution, _cloudfront_cost
from infra_cost_model.resources.rds import _rds_cost
from infra_cost_model.resources.s3 import S3Bucket, _s3_cost
from infra_cost_model.resources.sqs import _sqs_cost

DYNAMODB = "https://aws.amazon.com/dynamodb/pricing/on-demand/"
RDS_MYSQL = "https://aws.amazon.com/rds/mysql/pricing/"
EVENTBRIDGE = "https://aws.amazon.com/eventbridge/pricing/"
LAMBDA = "https://aws.amazon.com/lambda/pricing/"
SNS = "https://aws.amazon.com/sns/pricing/"
EC2_DATA_TRANSFER = "https://aws.amazon.com/ec2/pricing/on-demand/"
CLOUDFRONT = "https://aws.amazon.com/cloudfront/pricing/pay-as-you-go/"

M = 1_000_000
TB = 1024  # GB, as the AWS price list counts a terabyte
FREE_GB = 100  # data transfer out that AWS doesn't charge for each month

# (service, usage metric, quantity, cost in USD, source page)
DOCUMENTED = [
    # $0.125 per million read request units
    ("AmazonDynamoDB", "Dynamo-ReadRequest", M, 0.125, DYNAMODB),
    # $0.625 per million write request units
    ("AmazonDynamoDB", "Dynamo-WriteRequest", M, 0.625, DYNAMODB),
    # Single-AZ, MySQL: $0.017, $0.034 and $0.171 an hour
    ("AmazonRDS", "RDS-Instance-Hour-db.t3.micro", 730, 730 * 0.017, RDS_MYSQL),
    ("AmazonRDS", "RDS-Instance-Hour-db.t3.small", 730, 730 * 0.034, RDS_MYSQL),
    ("AmazonRDS", "RDS-Instance-Hour-db.m5.large", 730, 730 * 0.171, RDS_MYSQL),
    # $1.00 per million custom events, from the first event
    ("AmazonEventBridge", "EventBridge-CustomEvent", 500_000, 0.50, EVENTBRIDGE),
    ("AmazonEventBridge", "EventBridge-CustomEvent", 2 * M, 2.00, EVENTBRIDGE),
    # A scheduled rule on the default event bus costs nothing to run (#326)
    ("AmazonEventBridge", "EventBridge-Schedule", 20 * M, 0.0, EVENTBRIDGE),
    # Replayed events cost the same as custom events
    ("AmazonEventBridge", "EventBridge-ArchiveReplay", 2 * M, 2.00, EVENTBRIDGE),
    # x86 provisioned concurrency: $0.0000041667 per GB-second
    ("AWSLambda", "Lambda-ProvisionedConcurrency-GB-Second", M, 4.1667, LAMBDA),
    # SNS doesn't charge for deliveries to SQS or Lambda
    ("AmazonSNS", "SNS-Delivery-SQS", 2 * M, 0.0, SNS),
    ("AmazonSNS", "SNS-Delivery-Lambda", 2 * M, 0.0, SNS),
    # HTTP/S: first 100,000 free, then $0.60 per million
    ("AmazonSNS", "SNS-Delivery-HTTP", 1_100_000, 0.60, SNS),
    # Data transfer out to the internet: the first 100 GB a month free (#327),
    # then $0.09 up to 10 TB, $0.085, $0.07 and $0.05 per GB
    ("AWSDataTransfer", "DataTransfer-Internet-Out-GB", 50, 0.0, EC2_DATA_TRANSFER),
    ("AWSDataTransfer", "DataTransfer-Internet-Out-GB", 100, 0.0, EC2_DATA_TRANSFER),
    ("AWSDataTransfer", "DataTransfer-Internet-Out-GB", 150, 4.50, EC2_DATA_TRANSFER),
    ("AWSDataTransfer", "DataTransfer-Internet-Out-GB", 50 * TB,
     (10 * TB - FREE_GB) * 0.09 + 40 * TB * 0.085, EC2_DATA_TRANSFER),
    ("AWSDataTransfer", "DataTransfer-Internet-Out-GB", 200 * TB,
     (10 * TB - FREE_GB) * 0.09 + 40 * TB * 0.085 + 100 * TB * 0.07 + 50 * TB * 0.05,
     EC2_DATA_TRANSFER),
]


def seed_rows(service: str, metric: str) -> list[dict]:
    rows = json.loads(SEED_PRICES_PATH.read_text())
    return [r for r in rows if r["service"] == service and r["usage_metric"] == metric]


@pytest.mark.parametrize("service, metric, quantity, cost, source", DOCUMENTED,
                         ids=[f"{metric}-{quantity:g}" for _, metric, quantity, _, _ in DOCUMENTED])
def test_seed_row_matches_the_documented_price(seed_catalog, service, metric,
                                               quantity, cost, source):
    result = seed_catalog.query("aws", service, "us-east-1", metric, quantity)
    assert result is not None
    assert result.total_cost == pytest.approx(cost, rel=1e-6, abs=1e-9)


@pytest.mark.parametrize("service, metric, source",
                         sorted({(s, m, src) for s, m, _, _, src in DOCUMENTED}))
def test_corrected_rows_cite_the_aws_page(service, metric, source):
    rows = seed_rows(service, metric)
    assert rows
    for row in rows:
        assert row["source"] == source
        assert row["effective_date"] == "2026-09-23"


def test_multi_az_costs_twice_single_az(seed_catalog):
    """The instance rows are Single-AZ, so the 2.0 multiplier gives the
    Multi-AZ (one standby) price: $0.034 an hour for db.t3.micro."""
    single = _rds_cost(instance_class="db.t3.micro", storage_gb=0,
                       catalog=seed_catalog, region="us-east-1")
    multi = _rds_cost(instance_class="db.t3.micro", storage_gb=0, multi_az=True,
                      catalog=seed_catalog, region="us-east-1")
    assert single == pytest.approx(730 * 0.017)
    assert multi == pytest.approx(730 * 0.034)


def test_no_rows_under_the_rest_api_service_for_http_api_egress():
    rows = json.loads(SEED_PRICES_PATH.read_text())
    assert [r for r in rows if r["service"] == "AmazonAPIGateway"] == []


def test_http_api_egress_is_priced_as_data_transfer_out(seed_catalog):
    """An HTTP API's response bytes are billed as data transfer out."""
    data_transfer = seed_catalog.query("aws", "AWSDataTransfer", "us-east-1",
                                       "DataTransfer-Internet-Out-GB", 25 * TB)
    egress = _apigw_egress_cost(25 * TB, catalog=seed_catalog, region="us-east-1")
    assert egress == pytest.approx(data_transfer.total_cost)
    assert egress == pytest.approx((10 * TB - FREE_GB) * 0.09 + 15 * TB * 0.085)


# Rows removed because AWS doesn't have the charge they model.
REMOVED = [
    # The SQS page charges for requests and data transfer, not for storing
    # messages in a queue (#324).
    ("AmazonSQS", "SQS-Retention"),
    # CloudFront doesn't charge per origin fetch, and data transfer from an
    # AWS origin is free (#325).
    ("AmazonCloudFront", "CloudFront-OriginRequest-S3"),
    ("AmazonCloudFront", "CloudFront-OriginRequest-Custom"),
    # S3 egress to the internet is data transfer out, priced by the
    # account-wide DataTransfer-Internet-Out-GB rows (#332).
    ("AmazonS3", "S3-DataTransfer"),
]


@pytest.mark.parametrize("service, metric", REMOVED, ids=[m for _, m in REMOVED])
def test_seed_has_no_row_for_a_charge_aws_does_not_have(service, metric):
    assert seed_rows(service, metric) == []


def test_sqs_cost_has_no_storage_charge():
    """The SQS helper prices requests only (#324)."""
    assert "retention_gb" not in inspect.signature(_sqs_cost).parameters


def test_cloudfront_cost_has_no_origin_request_charge():
    """A CloudFront node has no origin fetch metric or price (#325)."""
    parameters = inspect.signature(_cloudfront_cost).parameters
    assert "origin_requests" not in parameters
    assert "origin_is_s3" not in parameters
    assert "originRequests" not in CloudFrontDistribution().valid_metrics


def egress_model(gb_per_node: dict[str, float]) -> dict:
    """One workflow run a month, and one data transfer node per entry."""
    nodes = {
        address: {
            "nodeType": "external",
            "provider": "aws",
            "service": "AWSDataTransfer",
            "region": "us-east-1",
            "usageMetrics": {"internetOutGb": {"unit": "GB", "value": gb}},
        }
        for address, gb in gb_per_node.items()
    }
    addresses = list(nodes)
    return {
        "version": "1.0",
        "workflow": {"name": "egress", "entry": addresses[0],
                     "frequency": {"unit": "perMonth", "value": 1}},
        "nodes": nodes,
        "edges": [{"from": a, "to": b, "type": "sync", "rate": 1.0}
                  for a, b in zip(addresses, addresses[1:])],
    }


def compute_egress(seed_catalog, gb_per_node: dict[str, float]) -> dict[str, float]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        engine = CostEngine(egress_model(gb_per_node), catalog=seed_catalog,
                            time_basis="monthly")
        return engine.compute()


@pytest.mark.parametrize("gb, cost", [(50, 0.0), (150, (150 - FREE_GB) * 0.09)])
def test_one_egress_node_gets_the_free_100_gb(seed_catalog, gb, cost):
    costs = compute_egress(seed_catalog, {"data_transfer.egress": gb})
    assert costs["data_transfer.egress"] == pytest.approx(cost, abs=1e-9)


def test_two_egress_nodes_share_one_free_100_gb(seed_catalog):
    """AWS sums data transfer out across the account, so two nodes of 60 GB
    pay for 20 GB and split the $1.80 in proportion to their quantities."""
    costs = compute_egress(seed_catalog, {"data_transfer.a": 60, "data_transfer.b": 60})
    assert sum(costs.values()) == pytest.approx((120 - FREE_GB) * 0.09)
    assert costs["data_transfer.a"] == pytest.approx(0.90)
    assert costs["data_transfer.b"] == pytest.approx(0.90)


# S3 egress to the internet (#332). The S3 page lists the first 100 GB a
# month free "aggregated across all AWS Services and Regions", and its rate
# tiers use the account's total data transfer out across AWS services.

@pytest.mark.parametrize("gb, cost", [
    (100, 0.0),
    (150, (150 - FREE_GB) * 0.09),
    (15_000, (10 * TB - FREE_GB) * 0.09 + (15_000 - 10 * TB) * 0.085),
])
def test_s3_egress_is_priced_as_data_transfer_out(seed_catalog, gb, cost):
    data_transfer = seed_catalog.query("aws", "AWSDataTransfer", "us-east-1",
                                       "DataTransfer-Internet-Out-GB", gb)
    egress = _s3_cost(data_out_gb=gb, catalog=seed_catalog, region="us-east-1")
    assert egress == pytest.approx(data_transfer.total_cost)
    assert egress == pytest.approx(cost)


def test_s3_handler_maps_data_out_to_the_data_transfer_rows():
    bucket = S3Bucket()
    assert bucket.catalog_metrics["dataOutGb"] == "DataTransfer-Internet-Out-GB"
    assert bucket.catalog_services == {"DataTransfer-Internet-Out-GB": "AWSDataTransfer"}


def egress_node(address: str, gb: float, metric: str = "dataOutGb") -> dict:
    if address.startswith("aws_s3_bucket."):
        service, node_type = "AmazonS3", "storage"
    else:
        service, node_type, metric = "AWSDataTransfer", "external", "internetOutGb"
    return {
        "nodeType": node_type,
        "resourceAddress": address,
        "provider": "aws",
        "service": service,
        "region": "us-east-1",
        "usageMetrics": {metric: {"unit": "GB", "value": gb}},
    }


def compute_nodes(seed_catalog, nodes: dict[str, dict]) -> tuple[dict, list]:
    addresses = list(nodes)
    model = {
        "version": "1.0",
        "workflow": {"name": "egress", "entry": addresses[0],
                     "frequency": {"unit": "perMonth", "value": 1}},
        "nodes": nodes,
        "edges": [{"from": a, "to": b, "type": "sync", "rate": 1.0}
                  for a, b in zip(addresses, addresses[1:])],
    }
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        costs = CostEngine(model, catalog=seed_catalog, time_basis="monthly").compute()
    unpriced = [w.message.unpriced.metric for w in caught
                if isinstance(w.message, UnpricedMetricWarning)]
    return costs, unpriced


@pytest.mark.parametrize("metric", ["dataOutGb", "S3-DataTransfer",
                                    "DataTransfer-Internet-Out-GB"])
@pytest.mark.parametrize("gb, cost", [(100, 0.0), (150, (150 - FREE_GB) * 0.09)])
def test_s3_node_egress_gets_the_free_100_gb(seed_catalog, metric, gb, cost):
    """The logical name, the old catalog name and the data transfer name
    all price from the data transfer rows."""
    address = "aws_s3_bucket.assets"
    costs, unpriced = compute_nodes(seed_catalog,
                                    {address: egress_node(address, gb, metric)})
    assert unpriced == []
    assert costs[address] == pytest.approx(cost, abs=1e-9)


def test_s3_and_api_egress_share_one_free_100_gb(seed_catalog):
    """An S3 bucket and an HTTP API's egress node each send 60 GB out.
    AWS sums them, so they pay for 20 GB and split the $1.80."""
    s3, api = "aws_s3_bucket.assets", "data_transfer.api_egress"
    costs, unpriced = compute_nodes(seed_catalog, {
        s3: egress_node(s3, 60), api: egress_node(api, 60)})
    assert unpriced == []
    assert costs[s3] + costs[api] == pytest.approx((120 - FREE_GB) * 0.09)
    assert costs[s3] == pytest.approx(0.90)
    assert costs[api] == pytest.approx(0.90)


def test_s3_and_api_egress_share_the_rate_tiers(seed_catalog):
    """Together they cross the 10 TB tier that neither reaches alone."""
    s3, api = "aws_s3_bucket.assets", "data_transfer.api_egress"
    costs, _ = compute_nodes(seed_catalog, {
        s3: egress_node(s3, 8 * TB), api: egress_node(api, 8 * TB)})
    expected = (10 * TB - FREE_GB) * 0.09 + 6 * TB * 0.085
    assert costs[s3] + costs[api] == pytest.approx(expected)


# CloudFront always-free tier (#333): 1 TB of data transfer out and
# 10,000,000 HTTP or HTTPS requests a month.

CLOUDFRONT_DOCUMENTED = [
    ("CloudFront-HTTPS-Request", 5 * M, 0.0),
    ("CloudFront-HTTPS-Request", 10 * M, 0.0),
    # $0.0100 per 10,000 HTTPS requests after the free 10 million
    ("CloudFront-HTTPS-Request", 12 * M, 2 * M * 0.0100 / 10_000),
    ("CloudFront-HTTP-Request", 5 * M, 0.0),
    ("CloudFront-HTTP-Request", 10 * M, 0.0),
    # $0.0075 per 10,000 HTTP requests after the free 10 million
    ("CloudFront-HTTP-Request", 12 * M, 2 * M * 0.0075 / 10_000),
    ("CloudFront-DataTransfer", 500, 0.0),
    ("CloudFront-DataTransfer", TB, 0.0),
    # United States, Mexico and Canada: next 9 TB at $0.085, next 40 TB at $0.080
    ("CloudFront-DataTransfer", 2 * TB, TB * 0.085),
    ("CloudFront-DataTransfer", 20 * TB, 9 * TB * 0.085 + 10 * TB * 0.080),
]


@pytest.mark.parametrize("metric, quantity, cost", CLOUDFRONT_DOCUMENTED,
                         ids=[f"{m}-{q:g}" for m, q, _ in CLOUDFRONT_DOCUMENTED])
def test_cloudfront_row_matches_the_documented_price(seed_catalog, metric, quantity, cost):
    result = seed_catalog.query("aws", "AmazonCloudFront", "global", metric, quantity)
    assert result is not None
    assert result.total_cost == pytest.approx(cost, rel=1e-6, abs=1e-9)


@pytest.mark.parametrize("metric", sorted({m for m, _, _ in CLOUDFRONT_DOCUMENTED}))
def test_cloudfront_rows_cite_the_aws_page(metric):
    rows = seed_rows("AmazonCloudFront", metric)
    assert rows
    for row in rows:
        assert row["source"] == CLOUDFRONT
        assert row["effective_date"] == "2026-09-23"


def test_cloudfront_issue_example_costs_nothing(seed_catalog):
    """5 million HTTPS requests and 500 GB out fit in the free tier."""
    cost = _cloudfront_cost(requests=5 * M, data_out_gb=500,
                            catalog=seed_catalog, region="global")
    assert cost == pytest.approx(0.0, abs=1e-9)


def test_cloudfront_http_and_https_share_the_free_10_million(seed_catalog):
    """8 million HTTP and 8 million HTTPS requests: 6 million are over the
    free 10 million, split in proportion between the two prices."""
    cost = _cloudfront_cost(requests=16 * M, https_ratio=0.5,
                            catalog=seed_catalog, region="global")
    assert cost == pytest.approx(3 * M * 0.0075 / 10_000 + 3 * M * 0.0100 / 10_000)
