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
"""

REGION = "region"
ACCOUNT = "account"

# AWS free tier: https://aws.amazon.com/free/ and each service's pricing page.
ACCOUNT_WIDE_FREE_TIERS: frozenset[tuple[str, str, str]] = frozenset({
    # The first 100 GB a month of data transfer out to the internet.
    ("aws", "AWSDataTransfer", "DataTransfer-Internet-Out-GB"),
    # 1,000,000 requests and 400,000 GB-seconds a month.
    ("aws", "AWSLambda", "Lambda-Request"),
    ("aws", "AWSLambda", "Lambda-GB-Second"),
    # 1,000,000 requests a month.
    ("aws", "AmazonSQS", "SQS-Standard-Request"),
    ("aws", "AmazonSQS", "SQS-FIFO-Request"),
    # 1,000,000 publishes and 100,000 HTTP deliveries a month.
    ("aws", "AmazonSNS", "SNS-Publish"),
    ("aws", "AmazonSNS", "SNS-Delivery-HTTP"),
})


def free_tier_scope(vendor: str | None, service: str | None,
                    usage_metric: str) -> str:
    """Return ``ACCOUNT`` when the metric's free allowance covers all regions.

    Every other metric returns ``REGION``: its tiers apply to each region's
    total on its own.
    """
    if (vendor, service, usage_metric) in ACCOUNT_WIDE_FREE_TIERS:
        return ACCOUNT
    return REGION
