"""Seed rows match AWS's published us-east-1 on-demand prices (#311).

Each case prices a quantity from the seed catalog and compares it with the
price on the AWS page that the row's ``source`` field names.
"""

import json

import pytest

from infra_cost_model.pricing.cache import SEED_PRICES_PATH
from infra_cost_model.resources.apigw import _apigw_egress_cost
from infra_cost_model.resources.rds import _rds_cost

DYNAMODB = "https://aws.amazon.com/dynamodb/pricing/on-demand/"
RDS_MYSQL = "https://aws.amazon.com/rds/mysql/pricing/"
EVENTBRIDGE = "https://aws.amazon.com/eventbridge/pricing/"
LAMBDA = "https://aws.amazon.com/lambda/pricing/"
SNS = "https://aws.amazon.com/sns/pricing/"
EC2_DATA_TRANSFER = "https://aws.amazon.com/ec2/pricing/on-demand/"

M = 1_000_000
TB = 1024  # GB, as the AWS price list counts a terabyte

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
    # Replayed events cost the same as custom events
    ("AmazonEventBridge", "EventBridge-ArchiveReplay", 2 * M, 2.00, EVENTBRIDGE),
    # x86 provisioned concurrency: $0.0000041667 per GB-second
    ("AWSLambda", "Lambda-ProvisionedConcurrency-GB-Second", M, 4.1667, LAMBDA),
    # SNS doesn't charge for deliveries to SQS or Lambda
    ("AmazonSNS", "SNS-Delivery-SQS", 2 * M, 0.0, SNS),
    ("AmazonSNS", "SNS-Delivery-Lambda", 2 * M, 0.0, SNS),
    # HTTP/S: first 100,000 free, then $0.60 per million
    ("AmazonSNS", "SNS-Delivery-HTTP", 1_100_000, 0.60, SNS),
    # Data transfer out to the internet: $0.09, $0.085, $0.07, $0.05 per GB
    ("AWSDataTransfer", "DataTransfer-Internet-Out-GB", 100, 9.00, EC2_DATA_TRANSFER),
    ("AWSDataTransfer", "DataTransfer-Internet-Out-GB", 50 * TB,
     10 * TB * 0.09 + 40 * TB * 0.085, EC2_DATA_TRANSFER),
    ("AWSDataTransfer", "DataTransfer-Internet-Out-GB", 200 * TB,
     10 * TB * 0.09 + 40 * TB * 0.085 + 100 * TB * 0.07 + 50 * TB * 0.05,
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
    assert egress == pytest.approx(10 * TB * 0.09 + 15 * TB * 0.085)
