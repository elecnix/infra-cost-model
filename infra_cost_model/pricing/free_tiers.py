"""Which free allowances a provider gives per region and which per account (#336).

Most catalog tiers apply to the account's use of a metric in one region. A
few providers give a free allowance once to the whole account, across all
regions. The engine applies such an allowance once and splits it across the
regions in proportion to their quantities. Each region still pays its own
rate for its paid usage.

The scope is a property of the provider's billing policy for a metric, not of
one region's price row. This table keys it on (vendor, service, usage
metric), so it covers every source that writes rows under those names: the
seed file, the AWS price list, Infracost and the vendor files. A row field
would need each source to set it, and live rows for a second region would
fall back to the regional default.

A few providers also give one free allowance to several metrics (#338). AWS
gives 1,000,000 free SQS requests a month to standard and FIFO queues
together, and CloudFront gives 10,000,000 free HTTP or HTTPS requests
(#339). ``SHARED_FREE_ALLOWANCES`` lists such groups, for the same reason
as above: live rows can't carry a field that says which metrics share an
allowance. The AWS price list states the SQS allowance as a product of its
own ("Global-Requests", queue type "Any"), which maps to no metric.
"""

from dataclasses import dataclass

REGION = "region"
ACCOUNT = "account"

# AWS free tier: https://aws.amazon.com/free/ and each service's pricing page.
ACCOUNT_WIDE_FREE_TIERS: frozenset[tuple[str, str, str]] = frozenset({
    # The first 100 GB a month of data transfer out to the internet.
    ("aws", "AWSDataTransfer", "DataTransfer-Internet-Out-GB"),
    # 1,000,000 requests and 400,000 GB-seconds a month.
    ("aws", "AWSLambda", "Lambda-Request"),
    ("aws", "AWSLambda", "Lambda-GB-Second"),
    # 1,000,000 publishes and 100,000 HTTP deliveries a month.
    ("aws", "AmazonSNS", "SNS-Publish"),
    ("aws", "AmazonSNS", "SNS-Delivery-HTTP"),
    # 20,000 requests a month "calculated across all Regions"
    # (https://aws.amazon.com/kms/pricing/). The AWS price list states it as
    # "Global-KMS-Requests", location "Any" (#343).
    ("aws", "AWSKMS", "KMS-API-Request"),
    # https://aws.amazon.com/cloudwatch/pricing/: 10 custom or detailed
    # monitoring metrics, 10 standard-resolution alarm metrics, and 5 GB each
    # of log ingestion and log storage a month. The AWS price list states
    # each as a "Global-" product with location "Any" (#342).
    ("aws", "AmazonCloudWatch", "CloudWatch-Metric-Month"),
    ("aws", "AmazonCloudWatch", "CloudWatch-Alarm-Month"),
    ("aws", "AmazonCloudWatch", "CloudWatch-Log-Ingestion"),
    ("aws", "AmazonCloudWatch", "CloudWatch-Log-Storage"),
    # https://azure.microsoft.com/en-us/pricing/details/functions/: the
    # consumption plan's free grant of 1 million requests and 400,000 GB-s is
    # "per month per subscription ... across all function apps in that
    # subscription" (#363).
    ("azure", "AzureFunctions", "AzureFunctions-Execution"),
    ("azure", "AzureFunctions", "AzureFunctions-GB-Second"),
    # https://cloud.google.com/run/pricing: "The free tier usage is
    # aggregated across projects by billing account and resets every
    # month" (#373).
    ("gcp", "CloudRun", "CloudRun-Request"),
    ("gcp", "CloudRun", "CloudRun-vCPU-Second"),
    ("gcp", "CloudRun", "CloudRun-GiB-Second"),
})


@dataclass(frozen=True)
class SharedFreeAllowance:
    """A monthly free allowance that covers several metrics of one service.

    The allowance covers the account's total use of all ``metrics``, in
    every region. ``unit`` is the unit of each metric's rows.
    """
    vendor: str
    service: str
    metrics: frozenset[str]
    allowance: float
    unit: str


SHARED_FREE_ALLOWANCES: tuple[SharedFreeAllowance, ...] = (
    # https://aws.amazon.com/sqs/pricing/: "All customers can make 1 million
    # Amazon SQS requests for free each month."
    SharedFreeAllowance(
        vendor="aws", service="AmazonSQS",
        metrics=frozenset({"SQS-Standard-Request", "SQS-FIFO-Request"}),
        allowance=1_000_000, unit="requests",
    ),
    # https://aws.amazon.com/cloudfront/pricing/pay-as-you-go/: "10,000,000
    # HTTP or HTTPS Requests per month" in the always-free tier (#339).
    SharedFreeAllowance(
        vendor="aws", service="AmazonCloudFront",
        metrics=frozenset({"CloudFront-HTTP-Request",
                           "CloudFront-HTTPS-Request"}),
        allowance=10_000_000, unit="requests",
    ),
)

_SHARED_BY_METRIC: dict[tuple[str, str, str], SharedFreeAllowance] = {
    (group.vendor, group.service, metric): group
    for group in SHARED_FREE_ALLOWANCES
    for metric in group.metrics
}


def shared_free_allowance(vendor: str | None, service: str | None,
                          usage_metric: str) -> SharedFreeAllowance | None:
    """Return the allowance group that covers the metric, or ``None``."""
    return _SHARED_BY_METRIC.get((vendor, service, usage_metric))


def free_tier_scope(vendor: str | None, service: str | None,
                    usage_metric: str) -> str:
    """Return ``ACCOUNT`` when the metric's free allowance covers all regions.

    A metric in a ``SHARED_FREE_ALLOWANCES`` group is account-wide too.
    Every other metric returns ``REGION``: its tiers apply to each region's
    total on its own.
    """
    if (vendor, service, usage_metric) in ACCOUNT_WIDE_FREE_TIERS:
        return ACCOUNT
    if shared_free_allowance(vendor, service, usage_metric) is not None:
        return ACCOUNT
    return REGION


# The monthly free allowance of each metric, in the unit of its rows (#356).
# The seed file states each one as a $0 tier from 0, and the metric's paid
# tiers start where the allowance ends. The Infracost Cloud Pricing API
# states only the paid prices, from 0: AWS publishes the allowances as
# separate "Global-" products that no descriptor selects. A live sync adds
# each allowance as a $0 tier, so a live catalog prices the same usage as the
# seed catalog does. A test checks that this table matches the seed file.
FREE_ALLOWANCES: dict[tuple[str, str, str], float] = {
    ("aws", "AWSDataTransfer", "DataTransfer-Internet-Out-GB"): 100,
    ("aws", "AWSLambda", "Lambda-Request"): 1_000_000,
    ("aws", "AWSLambda", "Lambda-GB-Second"): 400_000,
    ("aws", "AWSKMS", "KMS-API-Request"): 20_000,
    ("aws", "AmazonCloudFront", "CloudFront-DataTransfer"): 1024,
    ("aws", "AmazonCloudFront", "CloudFront-HTTP-Request"): 10_000_000,
    ("aws", "AmazonCloudFront", "CloudFront-HTTPS-Request"): 10_000_000,
    ("aws", "AmazonCloudWatch", "CloudWatch-Metric-Month"): 10,
    ("aws", "AmazonCloudWatch", "CloudWatch-Alarm-Month"): 10,
    ("aws", "AmazonCloudWatch", "CloudWatch-Log-Ingestion"): 5,
    ("aws", "AmazonCloudWatch", "CloudWatch-Log-Storage"): 5,
    ("aws", "AmazonSNS", "SNS-Publish"): 1_000_000,
    ("aws", "AmazonSNS", "SNS-Delivery-HTTP"): 100_000,
    ("aws", "AmazonSQS", "SQS-Standard-Request"): 1_000_000,
    ("aws", "AmazonSQS", "SQS-FIFO-Request"): 1_000_000,
    # https://aws.amazon.com/waf/pricing/: Bot Control includes 10 million
    # requests a month at the inspection level Common and 1 million at
    # Targeted, and Fraud Control 10,000 (#395). The AWS price list starts
    # each region's product, and each "Global-" product, with its own $0
    # tier, so the allowance applies to each region on its own.
    ("aws", "AWSWAF", "WAF-BotControl-Request"): 10_000_000,
    ("aws", "AWSWAF", "WAF-BotControl-Targeted-Request"): 1_000_000,
    ("aws", "AWSWAF", "WAF-FraudControl-Request"): 10_000,
    # Azure Retail Prices API (#363): the first tier of each meter is $0.
    ("azure", "AzureFunctions", "AzureFunctions-Execution"): 1_000_000,
    ("azure", "AzureFunctions", "AzureFunctions-GB-Second"): 400_000,
    ("azure", "APIManagement", "APIM-Consumption-Call"): 1_000_000,
    # https://azure.microsoft.com/pricing/details/bandwidth/: the first
    # 100 GB a month of internet egress are free (#372).
    ("azure", "Bandwidth", "Bandwidth-Internet-Out-GB"): 100,
    # https://cloud.google.com/run/pricing: 180,000 vCPU-seconds and 360,000
    # GiB-seconds a month for request-based billing (#373). The Infracost
    # rows state the 2M free requests themselves.
    ("gcp", "CloudRun", "CloudRun-vCPU-Second"): 180_000,
    ("gcp", "CloudRun", "CloudRun-GiB-Second"): 360_000,
    # https://cloud.google.com/firestore/pricing: 50,000 reads and 20,000
    # writes a day, and 1 GiB of stored data (#373). Catalog tiers count a
    # month, so a daily quota counts 365.25 / 12 = 30.4375 days, the month
    # of `SECONDS_PER_MONTH`. A day's unused quota doesn't carry over, so
    # this is exact only for usage spread evenly over the month.
    ("gcp", "Firestore", "Firestore-Read"): 50_000 * 30.4375,
    ("gcp", "Firestore", "Firestore-Write"): 20_000 * 30.4375,
    ("gcp", "Firestore", "Firestore-GiB-Month"): 1,
}

# Free allowances that a provider gives as a sum of money, stated as units
# at a reference price (#373). GCP applies the Cloud Run free tier "as a
# spending based discount using Tier 1 pricing", so in a Tier 2 region the
# allowance pays for fewer units. The value is the us-central1 price.
SPEND_BASED_FREE_TIERS: dict[tuple[str, str, str], float] = {
    ("gcp", "CloudRun", "CloudRun-vCPU-Second"): 0.000024,
    ("gcp", "CloudRun", "CloudRun-GiB-Second"): 0.0000025,
}

# Free tiers that a product states in every region but the provider gives in
# a few regions only. A live sync drops the $0 tier in the other regions.
# https://cloud.google.com/storage/pricing: "Cloud Storage Always Free quotas
# apply to usage in US-WEST1, US-CENTRAL1, and US-EAST1 regions" (#372).
FREE_ALLOWANCE_REGIONS: dict[tuple[str, str, str], tuple[str, ...]] = {
    ("gcp", "CloudStorage", "GCS-Internet-Egress-GiB"): (
        "us-central1", "us-east1", "us-west1"),
}
