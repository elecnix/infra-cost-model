"""Which metrics a provider bills once for the whole account (#378).

Most catalog tiers apply to one region's use of a metric, and each region
has its own prices. A global service, such as Route 53, bills the account's
use in every region together, at one price. The engine adds up such a
metric's quantity across all regions, prices the total once, and splits the
cost across the nodes by quantity.

The pool uses the rows stored under the region "global" when there are
some, then the us-east-1 rows, then the rows of one of the pool's regions.
A live sync stores the Route 53 rows under each sync region (#361), and
every copy has the same prices.

The scope is a property of the provider's billing policy, so this table in
the pricing layer states it, like ``ACCOUNT_WIDE_FREE_TIERS`` (#336). Live
rows come from sources that can't set a custom field on a row.
"""

# (vendor, service, usage metric)
GLOBAL_METRICS: frozenset[tuple[str, str, str]] = frozenset({
    # https://aws.amazon.com/route53/pricing/: $0.50 a month for each of the
    # first 25 hosted zones, then $0.10. The AWS price list states the
    # "HostedZone" product with location "Any" and no region.
    ("aws", "AmazonRoute53", "Route53-HostedZone"),
})

# The regions whose rows price a global pool, in order of preference.
GLOBAL_PRICE_REGIONS: tuple[str, ...] = ("global", "us-east-1")


def is_global_metric(vendor: str | None, service: str | None,
                     usage_metric: str) -> bool:
    """Return ``True`` when the provider bills the metric once per account."""
    return (vendor, service, usage_metric) in GLOBAL_METRICS
