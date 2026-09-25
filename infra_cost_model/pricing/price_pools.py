"""Which regions share one meter or SKU, and so its tiers (#389).

Most catalog tiers apply to one region's use of a metric. Some providers
bill a group of regions on one meter or SKU and count its tiers, and its
free allowance, on the group's total. The engine adds up such a metric's
quantity in each group, prices the total once, and splits the cost across
the nodes by quantity.

``PRICE_POOLS`` maps (vendor, service, usage metric) to a function that
names a region's group, or returns ``None`` for a region that the provider
bills on its own. Every region of a group has the same rows, since they
come from one meter or SKU. The pool uses the rows of its region that
comes first in alphabetical order and has some, so the choice doesn't
depend on node order.

The groups are a property of the provider's billing policy, so this table
in the pricing layer states them, like ``GLOBAL_METRICS`` (#378). Live rows
come from sources that can't set a custom field on a row.
"""

from typing import Callable, Optional

# Azure internet egress, routed over the Microsoft network. The Azure Retail
# Prices API (checked 2026-09-24, serviceName "Bandwidth", productName
# "Rtn Preference: MGN", meterName "Standard Data Transfer Out") gives these
# regions one meter for each zone. The zones match the ones on
# https://azure.microsoft.com/pricing/details/bandwidth/, except that the
# API puts israelcentral on the zone 1 meter, while the page lists
# Israel Central in zone 3. The engine prices the API rows, so it follows
# the API. The API gives newer regions, such as austriaeast, belgiumcentral
# and chilecentral, a meter of their own, so they are in no group.
_AZURE_EGRESS_ZONES: dict[str, str] = {
    # Zone 1, North America and Europe:
    # meter 9995d93a-7d35-4d3f-9c69-7a7fea447ef4.
    **dict.fromkeys((
        "canadacentral", "canadaeast", "centralus", "eastus", "eastus2",
        "francecentral", "francesouth", "germanynorth", "germanywestcentral",
        "israelcentral", "italynorth", "mexicocentral", "northcentralus",
        "northeurope", "norwayeast", "norwaywest", "southcentralus",
        "spaincentral", "swedencentral", "swedensouth", "switzerlandnorth",
        "switzerlandwest", "uksouth", "ukwest", "westcentralus", "westeurope",
        "westus", "westus2", "westus3",
    ), "zone-1"),
    # Zone 2, Asia and Oceania: meter fe167397-a38d-43c3-9bb3-8e2907e56a41.
    **dict.fromkeys((
        "australiacentral", "australiacentral2", "australiaeast",
        "australiasoutheast", "centralindia", "eastasia", "japaneast",
        "japanwest", "jioindiacentral", "jioindiawest", "koreacentral",
        "koreasouth", "qatarcentral", "southeastasia", "southindia",
        "westindia",
    ), "zone-2"),
    # Zone 3, South America, Africa and the Middle East:
    # meter c089a13a-9dd0-44b5-aa9e-44a77bbd6788.
    **dict.fromkeys((
        "brazilsouth", "southafricanorth", "southafricawest", "uaecentral",
        "uaenorth",
    ), "zone-3"),
}


def azure_egress_zone(region: str) -> Optional[str]:
    """The Azure egress zone whose meter bills ``region``, or ``None``."""
    return _AZURE_EGRESS_ZONES.get(region)


# Cloud Run internet egress. GCP counts "the monthly usage ... by each SKU"
# (https://cloud.google.com/vpc/network-pricing), and Cloud Run has one
# internet egress SKU for each continent, such as "Cloud Run Network
# Internet Data Transfer Out North America to North America"
# (https://cloud.google.com/skus/sku-groups/network-egress, checked
# 2026-09-24). The SKUs name seven continents: Africa, AsiaPacific,
# Europe, MiddleEast, North America, Oceania and South America. A GCP
# region name starts with its continent. https://cloud.google.com/run/pricing
# gives "1GiB free data transfer within North America per month", which the
# North America SKU states as its first tier.
_GCP_CONTINENT_PREFIXES: tuple[tuple[str, str], ...] = (
    ("us-", "north-america"),
    # northamerica-south1 (Mexico) is not here: no source says which SKU
    # bills it.
    ("northamerica-northeast", "north-america"),
    ("europe-", "europe"),
    ("me-", "middle-east"),
    ("asia-", "asia-pacific"),
    ("australia-", "oceania"),
    ("southamerica-", "south-america"),
    ("africa-", "africa"),
)


def gcp_continent(region: str) -> Optional[str]:
    """The continent of the GCP SKU that bills ``region``, or ``None``."""
    for prefix, continent in _GCP_CONTINENT_PREFIXES:
        if region.startswith(prefix):
            return continent
    return None


# (vendor, service, usage metric) -> the region's group.
PRICE_POOLS: dict[tuple[str, str, str], Callable[[str], Optional[str]]] = {
    ("azure", "Bandwidth", "Bandwidth-Internet-Out-GB"): azure_egress_zone,
    ("gcp", "CloudRun", "CloudRun-Internet-Egress-GiB"): gcp_continent,
}

# GCS-Internet-Egress-GiB is not in the table. Its SKU, "Download Worldwide
# Destinations (excluding Asia & Australia)", covers every region, but the
# 100 GiB of Always Free egress applies to 3 US regions only
# (`FREE_ALLOWANCE_REGIONS`), so their rows differ from the others (#404).


def price_pool(vendor: Optional[str], service: Optional[str],
               usage_metric: str, region: Optional[str]) -> Optional[str]:
    """Name the group of regions whose use of the metric shares its tiers.

    Returns ``None`` when the metric has no groups, or when the provider
    bills ``region`` on its own.
    """
    group_of = PRICE_POOLS.get((vendor, service, usage_metric))
    if group_of is None or region is None:
        return None
    return group_of(region)
