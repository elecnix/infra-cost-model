"""Infracost Cloud Pricing API client.

Talks to the real Infracost Cloud Pricing API (a GraphQL endpoint) and maps each
result into the catalog's ``Price`` rows.

Auth: both CLI/CI tokens and logged-in session tokens authenticate as a Bearer
token plus an ``x-infracost-org-id`` header. The token + org id resolve from, in
order: explicit constructor args, the ``INFRACOST_API_KEY`` / ``INFRACOST_ORG_ID``
env vars, then the logged-in ``infracost auth login`` session files.

If no credential is present (or a live query fails), callers fall back to the
bundled seed price list — but loudly (a ``UserWarning``), never silently, so a
broken live path can't masquerade as success.
"""

import dataclasses
import os
import json
import re
import platform
import warnings
import requests
from pathlib import Path
from datetime import datetime
from typing import Optional

from infra_cost_model.pricing.free_tiers import (
    FREE_ALLOWANCES, FREE_ALLOWANCE_REGIONS, SPEND_BASED_FREE_TIERS,
)
from infra_cost_model.pricing.sources import azure_retail

# The real hosted Cloud Pricing API (GraphQL). Override for self-hosting/tests.
INFRACOST_PRICING_API_URL = os.getenv(
    "INFRACOST_PRICING_API_ENDPOINT", "https://pricing.api.infracost.io/graphql"
)

_PRICES_QUERY = """
query($vendorName: String!, $service: String!, $region: String!,
      $productFamily: String, $attributeFilters: [AttributeFilter!],
      $purchaseOption: String) {
  products(filter: {
    vendorName: $vendorName, service: $service, region: $region,
    productFamily: $productFamily, attributeFilters: $attributeFilters
  }) {
    productFamily
    attributes { key value }
    prices(filter: { purchaseOption: $purchaseOption }) {
      USD
      unit
      startUsageAmount
      endUsageAmount
    }
  }
}
"""


def _infracost_config_dir() -> Path:
    """Locate the infracost CLI config dir across platforms.

    The CLI stores ``token.json`` / ``user.json`` here. ``INFRACOST_CONFIG_DIR``
    overrides; otherwise macOS uses ``~/Library/Application Support/infracost`` and
    other platforms the XDG ``~/.config/infracost``.
    """
    override = os.getenv("INFRACOST_CONFIG_DIR")
    if override:
        return Path(override)
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "infracost"
    xdg = os.getenv("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "infracost"


def _to_float(value) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# Region prefix for AWS usagetype attribute values. Not exhaustive; extend as needed.
_REGION_PREFIX = {
    "us-east-1": "USE1", "us-east-2": "USE2", "us-west-1": "USW1",
    "us-west-2": "USW2", "ca-central-1": "CAN1", "ca-west-1": "CAN2",
    "eu-west-1": "EU", "eu-west-2": "EUW2", "eu-west-3": "EUW3",
    "eu-central-1": "EUC1", "eu-central-2": "EUC2", "eu-north-1": "EUN1",
    "eu-south-1": "EUS1", "eu-south-2": "EUS2",
    "ap-southeast-1": "APS1", "ap-southeast-2": "APS2", "ap-southeast-3": "APS4",
    "ap-south-1": "APS3", "ap-south-2": "APS5",
    "ap-northeast-1": "APN1", "ap-northeast-2": "APN2", "ap-northeast-3": "APN3",
    "ap-east-1": "APE1",
    "sa-east-1": "SAE1",
    "me-south-1": "MES1", "me-central-1": "MEC1",
    "af-south-1": "AFS1",
    "il-central-1": "ILC1",
}


def _region_usagetype_prefix(region: str) -> str:
    """Return the AWS usagetype region prefix for *region* (e.g. ``CAN1``).

    Falls back to ``REGION_PREFIX`` so a missing entry still produces a valid
    GraphQL variable but the query will return empty.
    """
    return _REGION_PREFIX.get(region, "REGION_PREFIX")


# Azure regions to sync (#226). Infracost names an Azure region by its ARM name
# (eastus), which is also the `location` value the Azure handlers read.
_AZURE_REGIONS = (
    "eastus", "eastus2", "centralus", "northcentralus", "southcentralus",
    "westcentralus", "westus", "westus2", "westus3", "canadacentral", "canadaeast",
    "brazilsouth", "mexicocentral", "northeurope", "westeurope", "uksouth", "ukwest",
    "francecentral", "germanywestcentral", "switzerlandnorth", "norwayeast",
    "swedencentral", "italynorth", "polandcentral", "spaincentral", "eastasia",
    "southeastasia", "japaneast", "japanwest", "koreacentral", "australiaeast",
    "australiasoutheast", "centralindia", "southindia", "uaenorth",
    "southafricanorth", "qatarcentral", "israelcentral",
)

# GCP location name for each region (#226). Infracost keeps some GCP products,
# such as Firestore operations, in its global catalogue and names the location
# in the description ("Cloud Firestore Read Ops Iowa"). `GCP_LOCATION` in a
# descriptor's filter value is replaced with this name, the way REGION_PREFIX is
# for AWS. The keys are also the GCP regions to sync.
_GCP_LOCATION = {
    "us-central1": "Iowa", "us-east1": "South Carolina", "us-east4": "Northern Virginia",
    "us-east5": "Columbus", "us-south1": "Dallas", "us-west1": "Oregon",
    "us-west2": "Los Angeles", "us-west4": "Las Vegas",
    "northamerica-northeast1": "Montreal", "northamerica-northeast2": "Toronto",
    "southamerica-east1": "Sao Paulo", "southamerica-west1": "Santiago",
    "europe-west1": "Belgium", "europe-west2": "London", "europe-west3": "Frankfurt",
    "europe-west4": "Netherlands", "europe-west8": "Milan", "europe-west9": "Paris",
    "europe-west10": "Berlin", "europe-west12": "Turin", "europe-southwest1": "Madrid",
    "europe-north1": "Finland", "me-central1": "Doha", "me-central2": "Dammam",
    "me-west1": "Tel Aviv", "asia-east1": "Taiwan", "asia-east2": "Hong Kong",
    "asia-northeast1": "Tokyo", "asia-northeast2": "Osaka", "asia-northeast3": "Seoul",
    "asia-south1": "Mumbai", "asia-south2": "Delhi", "asia-southeast1": "Singapore",
    "asia-southeast2": "Jakarta", "australia-southeast1": "Sydney",
    "australia-southeast2": "Melbourne", "africa-south1": "Johannesburg",
}


# The catalog region of AWS prices that have no region, such as those of a
# CloudFront distribution or of a web ACL with the scope CLOUDFRONT (#385).
# The Cloud Pricing API keeps them in its global catalogue (region ""), with
# the usagetype prefix "Global-". Only descriptors with `global_scope` sync it.
GLOBAL_REGION = "global"
_GLOBAL_USAGETYPE_PREFIX = "Global"

# Cloud Run regions with Tier 2 prices, from https://cloud.google.com/run/pricing
# (checked 2026-09-24). Infracost names a region's tier in the description
# ("Services CPU Tier 2 (Request-based billing)"), and gives africa-south1,
# a Tier 1 region, both products (#376). `CLOUD_RUN_TIER` in a descriptor's
# pattern is replaced with " Tier 2" in these regions, and with nothing in
# the others, so the pattern matches the tier that GCP publishes.
_CLOUD_RUN_TIER_2_REGIONS = frozenset({
    "asia-east2", "asia-northeast3", "asia-southeast1", "asia-southeast2", "asia-south2",
    "australia-southeast1", "australia-southeast2", "europe-central2", "europe-west10",
    "europe-west12", "europe-west2", "europe-west3", "europe-west6", "me-central1",
    "me-central2", "northamerica-northeast1", "northamerica-northeast2",
    "southamerica-east1", "southamerica-west1", "us-west2", "us-west3", "us-west4",
})


def sync_regions(vendor: str) -> list[str]:
    """The regions that ``sync-pricing`` syncs for *vendor* by default.

    The AWS list ends with ``GLOBAL_REGION``.
    """
    regions = {"azure": _AZURE_REGIONS, "gcp": _GCP_LOCATION}.get(vendor)
    if regions is None:
        return sorted(_REGION_PREFIX) + [GLOBAL_REGION]
    return sorted(regions)


def _resolve_gcp_location(filters: Optional[list[dict]], region: str) -> Optional[list[dict]]:
    """Replace ``GCP_LOCATION`` in filter values with *region*'s location name.

    An unknown region keeps the placeholder, so the query matches nothing.
    """
    if not filters:
        return filters
    location = _GCP_LOCATION.get(region, "GCP_LOCATION")
    return [{**f, "value": f["value"].replace("GCP_LOCATION", location)} for f in filters]


def _resolve_cloud_run_tier(patterns: dict, region: str) -> dict:
    """Replace ``CLOUD_RUN_TIER`` in pattern values with *region*'s tier."""
    tier = " Tier 2" if region in _CLOUD_RUN_TIER_2_REGIONS else ""
    return {key: pattern.replace("CLOUD_RUN_TIER", tier)
            for key, pattern in patterns.items()}


def _scaled(amount: Optional[float], scale: float) -> Optional[float]:
    return None if amount is None else amount * scale


def _close_open_tiers(prices: list[dict]) -> list[dict]:
    """End each tier where the next tier starts, when the API gives no end.

    Azure tiers come with a start and no end. The catalog charges a tier
    without an end for every unit above its start, so each tier but the last
    needs the next tier's start as its end.
    """
    def start(p):
        return p.get("start_usage_amount") or 0

    ordered = sorted(prices, key=start)
    closed = []
    for p in ordered:
        # The next tier is the first one that starts above this one.
        later = [start(q) for q in ordered if start(q) > start(p)]
        if p.get("end_usage_amount") is None and later:
            p = {**p, "end_usage_amount": min(later)}
        closed.append(p)
    return closed


class InfracostClient:
    """GraphQL client for the Infracost Cloud Pricing API."""

    def __init__(self, api_url: str = None, api_key: str = None, org_id: str = None):
        self.api_url = api_url or INFRACOST_PRICING_API_URL
        self._api_key = api_key
        self._org_id = org_id
        self._session_token: Optional[str] = None
        self._session_org: Optional[str] = None
        self._session_loaded = False

    def _load_session(self) -> None:
        """Load the CLI session token + org id from the infracost config dir (once)."""
        if self._session_loaded:
            return
        self._session_loaded = True
        cfg = _infracost_config_dir()
        try:
            tok = json.loads((cfg / "token.json").read_text())
            # infracost writes snake_case `access_token`.
            self._session_token = tok.get("access_token") or tok.get("accessToken")
        except (OSError, json.JSONDecodeError):
            pass
        try:
            usr = json.loads((cfg / "user.json").read_text())
            orgs = usr.get("organizations") or []
            if orgs:
                self._session_org = orgs[0].get("id")
        except (OSError, json.JSONDecodeError):
            pass

    def auth_headers(self) -> Optional[dict]:
        """Return Bearer + org-id auth headers, or None if no usable credential.

        Resolves the token and org id from constructor args, then the
        INFRACOST_API_KEY / INFRACOST_ORG_ID env vars, then the logged-in session.
        """
        token = self._api_key or os.getenv("INFRACOST_API_KEY")
        org = self._org_id or os.getenv("INFRACOST_ORG_ID")
        if not (token and org):
            self._load_session()
            token = token or self._session_token
            org = org or self._session_org
        if token and org:
            return {
                "Authorization": f"Bearer {token}",
                "x-infracost-org-id": org,
            }
        return None

    def is_authenticated(self) -> bool:
        return self.auth_headers() is not None

    def query_prices(self, service: str, region: str,
                     product_family: str = None,
                     attribute_filters: list[dict] = None,
                     purchase_option: str = None,
                     vendor: str = "aws") -> list[dict]:
        """Query the Cloud Pricing API and return flattened price records.

        Args mirror Infracost's ``products`` filter. Returns a list of dicts with
        ``unit``, ``price_usd``, the product family/attributes, and tier bounds.
        """
        headers = self.auth_headers()
        if headers is None:
            raise RuntimeError(
                "Infracost auth not found. Set INFRACOST_API_KEY (recommended) or "
                "run 'infracost auth login'."
            )
        headers["Content-Type"] = "application/json"

        variables = {
            "vendorName": vendor,
            "service": service,
            "region": region,
        }
        if product_family:
            variables["productFamily"] = product_family
        if attribute_filters:
            # AWS usagetype values encode the region as a prefix
            # (USE1- / CAN1- / EU- / APS2- / …) and break across regions
            # otherwise. `REGION_PREFIX` is replaced here before the query.
            region_prefix = _region_usagetype_prefix(region)
            resolved = []
            for f in attribute_filters:
                val = f["value"]
                if "REGION_PREFIX" in val:
                    val = val.replace("REGION_PREFIX", region_prefix)
                resolved.append({"key": f["key"], "value": val})
            variables["attributeFilters"] = resolved
        if purchase_option:
            variables["purchaseOption"] = purchase_option
        response = requests.post(
            self.api_url,
            headers=headers,
            json={"query": _PRICES_QUERY, "variables": variables},
            timeout=30,
        )
        if response.status_code in (401, 403):
            raise RuntimeError(
                f"Infracost auth rejected (HTTP {response.status_code}). Check "
                f"INFRACOST_API_KEY or re-run 'infracost auth login'. "
                f"Response: {response.text[:200]}"
            )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise RuntimeError(f"Infracost API returned errors: {payload['errors']}")

        results = []
        for product in payload.get("data", {}).get("products", []) or []:
            attributes = {
                a.get("key"): a.get("value")
                for a in product.get("attributes", []) or []
            }
            for price in product.get("prices", []) or []:
                usd = _to_float(price.get("USD"))
                if usd is None:
                    continue
                results.append({
                    "vendor": vendor,
                    "service": service,
                    "region": region,
                    "product_family": product.get("productFamily"),
                    "attributes": attributes,
                    "unit": price.get("unit"),
                    "price_usd": usd,
                    "start_usage_amount": _to_float(price.get("startUsageAmount")),
                    "end_usage_amount": _to_float(price.get("endUsageAmount")),
                    "source": "infracost",
                })
        return results

    def sync_to_cache(self, cache, usage_metric: str, region: str,
                      vendor: str = "aws") -> int:
        """Fetch the prices for one catalog usage_metric and store them.

        Resolves the metric to an Infracost product descriptor (service, family,
        attribute filters, purchase option, unit) and stores the matching prices
        under the catalog's ``usage_metric`` name.

        The new rows replace the metric's Infracost rows for the region, in
        one transaction (#355). Rows from a product that the descriptor no
        longer selects go away, and rows from other sources stay. When the
        metric has a free allowance in ``FREE_ALLOWANCES``, the stored rows
        start with it as a $0 tier (#356).

        Returns the number of rows stored.
        """
        descriptor = METRIC_DESCRIPTORS.get(usage_metric)
        if descriptor is None:
            raise KeyError(f"No Infracost descriptor for usage_metric '{usage_metric}'")
        # Azure and GCP descriptors name their vendor (#226).
        vendor = descriptor.get("vendor", vendor)

        # Some services (notably AWSDataTransfer) catalogue their products
        # globally, with region="". `query_region` lets a descriptor query that
        # global catalogue while the price is still stored under the caller's
        # `region` (see `region_pair_source` below).
        query_region = descriptor.get("query_region", region)
        attribute_filters = descriptor.get("attribute_filters")
        if region == GLOBAL_REGION:
            # The global products, such as those of a web ACL with the scope
            # CLOUDFRONT, are in the global catalogue with the usagetype
            # prefix "Global-" (#385).
            if not descriptor.get("global_scope"):
                raise KeyError(f"The descriptor for '{usage_metric}' has no global product")
            query_region = ""
            attribute_filters = [
                {"key": f["key"],
                 "value": f["value"].replace("REGION_PREFIX", _GLOBAL_USAGETYPE_PREFIX)}
                for f in attribute_filters or []
            ]
        if (attribute_filters and descriptor.get("unprefixed_in_us_east_1")
                and query_region == "us-east-1"):
            # Some services name the us-east-1 product without a region prefix
            # ("LoadBalancerUsage", not "USE1-LoadBalancerUsage").
            attribute_filters = [
                {"key": f["key"], "value": f["value"].replace("REGION_PREFIX-", "")}
                for f in attribute_filters
            ]
        if descriptor.get("azure_retail"):
            # Infracost's copy of this meter has stale tiers (#372).
            prices = []
        else:
            prices = self.query_prices(
                service=descriptor["service"],
                region=query_region,
                product_family=descriptor.get("product_family"),
                attribute_filters=_resolve_gcp_location(attribute_filters, region),
                purchase_option=descriptor.get("purchase_option"),
                vendor=vendor,
            )
        unit_match = descriptor.get("unit")
        if vendor == "azure" and not _with_unit(prices, unit_match):
            # Infracost lacks some Azure meters in some regions (#376). The
            # public Azure Retail Prices API, which Infracost copies, has them.
            prices = azure_retail.query_azure_retail_prices(
                descriptor["service"], region, attribute_filters)
        if descriptor.get("attribute_patterns"):
            descriptor = {**descriptor, "attribute_patterns": _resolve_cloud_run_tier(
                descriptor["attribute_patterns"], region)}
        now = datetime.now().isoformat()
        # Some products are priced by Infracost under a different service than the
        # handler/seed model them (e.g. NAT Gateway is priced under AmazonEC2 but
        # modeled under AmazonVPC). `store_service` stores them under the service
        # the engine queries.
        store_service = descriptor.get("store_service") or descriptor["service"]

        if descriptor.get("region_pair_source"):
            rows = self._region_pair_representative(
                prices, usage_metric, region, unit_match, descriptor, now)
        elif descriptor.get("regionless_usagetype"):
            rows = self._regionless_usagetype(
                prices, usage_metric, region, unit_match, descriptor, now)
        else:
            rows = self._one_product(prices, usage_metric, unit_match, descriptor, now)

        # Rows from the global catalogue (query_region "") are stored under the
        # sync region too, where the engine looks for them. `store_unit` gives
        # the rows the unit that the seed file states (#367).
        changes = {"service": store_service, "region": region}
        if descriptor.get("store_unit"):
            changes["unit"] = descriptor["store_unit"]
        rows = [dataclasses.replace(r, **changes) for r in rows]
        key = (vendor, store_service, usage_metric)
        free_regions = FREE_ALLOWANCE_REGIONS.get(key)
        if free_regions is not None and region not in free_regions:
            # The product states a free tier that GCP gives in a few regions.
            rows = _without_free_tier(rows)
        rows = _with_free_tier(rows, FREE_ALLOWANCES.get(key),
                               SPEND_BASED_FREE_TIERS.get(key))
        # Rows from the Azure Retail Prices API replace Infracost rows too.
        with cache.replacing(vendor, store_service, region, usage_metric,
                             ("infracost", azure_retail.SOURCE)):
            for row in rows:
                cache.upsert(row)
        return len(rows)

    @staticmethod
    def _one_product(prices, usage_metric, unit_match, descriptor, now) -> list:
        """Keep the rows of the one product that the descriptor selects.

        `usagetype_exclude` drops sibling usagetypes that share the same unit
        (e.g. NAT Gateway's $0 "Prvd" provisioned rows).
        """
        excludes = descriptor.get("usagetype_exclude") or []
        # `attribute_patterns` keeps the rows whose attributes match a regular
        # expression, for values the API can only match exactly. GCP names a
        # region's price tier in the description ("Services CPU Tier 2 ...").
        patterns = descriptor.get("attribute_patterns") or {}
        kept = []
        for p in _with_unit(prices, unit_match):
            attributes = p.get("attributes") or {}
            usagetype = attributes.get("usagetype", "")
            if any(x in usagetype for x in excludes):
                continue
            if not all(re.fullmatch(pattern, attributes.get(key) or "")
                       for key, pattern in patterns.items()):
                continue
            kept.append(p)
        _require_one_product(usage_metric, kept)
        kept = _close_open_tiers(kept)
        # Azure prices some meters per block of units, such as $0.035 per 10K
        # API calls. `unit_scale` is the block size: the stored row prices one
        # unit, and its tier bounds count units. A scale below 1 does the
        # opposite: 1e-6 turns a price per query into a price per million.
        scale = descriptor.get("unit_scale", 1)
        return [
            _price_row({**p, "price_usd": p["price_usd"] / scale,
                        "start_usage_amount": _scaled(p.get("start_usage_amount"), scale),
                        "end_usage_amount": _scaled(p.get("end_usage_amount"), scale)},
                       usage_metric, now)
            for p in kept
        ]

    @staticmethod
    def _region_pair_representative(prices, usage_metric, region, unit_match,
                                    descriptor, now) -> list:
        """Collapse per-region-pair prices to one representative rate.

        Data-transfer products are priced per source/destination region pair, with
        the source region encoded as the usagetype prefix (e.g. ``USE1-APS4-AWS-
        Out-Bytes``). A single catalog metric like ``DataTransfer-InterRegion-GB``
        models a generic rate, so we:

        1. keep only rows leaving THIS region (usagetype starts with the region's
           short prefix) and matching the descriptor's usagetype suffix,
        2. drop $0 rows (Local Zones / Wavelength / same-metro pairs),
        3. pick the modal price — the region's standard published rate — and store
           it once, flat, under the caller's ``region``.

        Returns the rows to store (none or one).
        """
        from collections import Counter

        prefix = _region_usagetype_prefix(region)
        suffix = descriptor.get("usagetype_suffix", "-AWS-Out-Bytes")
        candidates = []
        for p in _with_unit(prices, unit_match):
            usagetype = (p.get("attributes") or {}).get("usagetype", "")
            if not usagetype.startswith(f"{prefix}-") or not usagetype.endswith(suffix):
                continue
            if (p.get("price_usd") or 0) <= 0:
                continue
            candidates.append(p)

        if not candidates:
            return []

        # Modal price = the region's standard rate; tie-break toward the lower rate.
        counts = Counter(round(p["price_usd"], 6) for p in candidates)
        modal = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]
        rep = next(p for p in candidates if round(p["price_usd"], 6) == modal)
        return [dataclasses.replace(_price_row(rep, usage_metric, now),
                                    start_usage_amount=None, end_usage_amount=None)]

    @staticmethod
    def _regionless_usagetype(prices, usage_metric, region, unit_match,
                              descriptor, now) -> list:
        """Store a globally-catalogued, single-usagetype metric under the region.

        Unlike inter-region transfer, internet egress and inter-AZ transfer have
        ONE usagetype per source region (not per region pair), but still live in
        the global (region="") ``AWSDataTransfer`` catalogue. This keeps every row
        for the region's usagetype — preserving tiers (internet egress is tiered
        $0.09 / $0.085 / $0.07 / $0.05) — and stores them under the sync ``region``.

        us-east-1 data-transfer usagetypes are unprefixed (an AWS legacy quirk);
        every other region prepends its short prefix (e.g. ``USW1-``).

        Returns the rows to store.
        """
        base = descriptor["usagetype_base"]
        prefix = _region_usagetype_prefix(region)
        target = base if region == "us-east-1" else f"{prefix}-{base}"
        return [
            _price_row(p, usage_metric, now) for p in _with_unit(prices, unit_match)
            if (p.get("attributes") or {}).get("usagetype") == target
        ]


def _with_unit(prices: list[dict], unit_match) -> list[dict]:
    """Keep the prices whose unit is *unit_match* (#360).

    *unit_match* is one unit, a list of units, or ``None`` for any unit.
    The Cloud Pricing API spells the unit of one product differently in
    each region, and gives some products in two spellings at the same
    prices. A list names the spellings in order of preference, and this
    keeps the prices of the first spelling that *prices* have, so each tier
    is stored once.
    """
    if not unit_match:
        return prices
    spellings = [unit_match] if isinstance(unit_match, str) else unit_match
    present = {p.get("unit") for p in prices}
    for unit in spellings:
        if unit in present:
            return [p for p in prices if p.get("unit") == unit]
    return []


def _price_row(p: dict, usage_metric: str, now: str):
    """Turn one flattened Infracost price into a cache row for *usage_metric*."""
    from infra_cost_model.pricing.cache import Price

    return Price(
        vendor=p["vendor"], service=p["service"], region=p["region"],
        product_family=p.get("product_family"), attributes=p.get("attributes") or {},
        usage_metric=usage_metric, unit=p["unit"], price_usd=p["price_usd"],
        start_usage_amount=p.get("start_usage_amount"),
        end_usage_amount=p.get("end_usage_amount"),
        source=p.get("source", "infracost"), effective_date=now, fetched_at=now,
    )


def _with_free_tier(rows: list, allowance: float | None,
                    reference_price: float | None = None) -> list:
    """Start *rows* with a $0 tier for the metric's free allowance (#356).

    Infracost states a metric's paid tiers from 0. The seed file states the
    allowance as a $0 tier from 0 to the allowance, and the paid tiers from
    there. This gives the live rows the same tiers: it drops the parts of
    the paid tiers below the allowance and adds the $0 tier, with the
    product family and attributes of the paid rows. With no allowance, or
    no rows, it returns *rows* as they are.

    With a *reference_price*, the allowance is worth ``allowance *
    reference_price`` dollars (#373). Where the first paid price is higher,
    the $0 tier holds as many units as that sum pays for.
    """
    if not allowance or not rows:
        return rows
    ordered = sorted(rows, key=lambda r: r.start_usage_amount or 0)
    paid_prices = [r.price_usd for r in ordered if r.price_usd > 0]
    if reference_price and paid_prices and paid_prices[0] > reference_price:
        allowance = allowance * reference_price / paid_prices[0]
    paid = [
        dataclasses.replace(r, start_usage_amount=max(r.start_usage_amount or 0,
                                                      allowance))
        for r in ordered
        if r.end_usage_amount is None or r.end_usage_amount > allowance
    ]
    free = dataclasses.replace(ordered[0], price_usd=0.0, start_usage_amount=0.0,
                               end_usage_amount=float(allowance))
    return [free] + paid


def _without_free_tier(rows: list) -> list:
    """Drop the leading $0 tier of *rows*, and start the next tier at 0."""
    ordered = sorted(rows, key=lambda r: r.start_usage_amount or 0)
    if len(ordered) < 2 or ordered[0].price_usd != 0 or (ordered[0].start_usage_amount or 0):
        return rows
    return [dataclasses.replace(ordered[1], start_usage_amount=0.0)] + ordered[2:]


def _require_one_product(usage_metric: str, prices: list[dict]) -> None:
    """Raise if *prices* come from more than one product (#350).

    A metric's rows are the tiers of one product. Rows from several products
    would be stored as tiers that overlap, and a query would charge the
    quantity once for each product. Two rows belong to the same product when
    their product family and attributes are equal.
    """
    products = {
        (p.get("product_family"), tuple(sorted((p.get("attributes") or {}).items())))
        for p in prices
    }
    if len(products) > 1:
        # Azure rows name the product by meter, GCP rows by description.
        usagetypes = sorted(
            next((dict(attrs)[k] for k in ("usagetype", "meterName", "description")
                  if k in dict(attrs)), "?")
            for _, attrs in products)
        raise RuntimeError(
            f"Infracost descriptor for '{usage_metric}' matched {len(products)} "
            f"products (usagetypes: {', '.join(usagetypes)}); it must match one. "
            f"No rows were stored."
        )


# Map each catalog usage_metric to the Infracost product query that prices it.
# Validated against the live Cloud Pricing API; extend per service as needed.
METRIC_DESCRIPTORS: dict[str, dict] = {
    # Lambda (#360): the usagetype selects the product in each region, bare
    # in us-east-1. The API spells the units differently in each region, and
    # gives some products in two spellings at the same prices ("Requests" and
    # "Request" in eu-west-1; "seconds", "Second" and "Lambda-GB-Second" for
    # GB-seconds). The sync keeps the first spelling in the list that the
    # product has, and stores the rows with the seed file's unit (#367).
    "Lambda-Request": {
        "service": "AWSLambda", "product_family": "Serverless",
        "attribute_filters": [{"key": "group", "value": "AWS-Lambda-Requests"},
                              {"key": "usagetype", "value": "REGION_PREFIX-Request"}],
        "unprefixed_in_us_east_1": True,
        "purchase_option": "on_demand", "unit": ["Requests", "Request"],
        "store_unit": "requests",
    },
    "Lambda-GB-Second": {
        "service": "AWSLambda", "product_family": "Serverless",
        "attribute_filters": [{"key": "group", "value": "AWS-Lambda-Duration"},
                              {"key": "usagetype", "value": "REGION_PREFIX-Lambda-GB-Second"}],
        "unprefixed_in_us_east_1": True,
        "purchase_option": "on_demand",
        "unit": ["Lambda-GB-Second", "seconds", "Second"],
        "store_unit": "GB-s",
    },
    "Dynamo-WriteRequest": {
        "service": "AmazonDynamoDB", "product_family": "Amazon DynamoDB PayPerRequest Throughput",
        "attribute_filters": [{"key": "group", "value": "DDB-WriteUnits"}],
        "purchase_option": "on_demand", "unit": "WriteRequestUnits",
    },
    "Dynamo-ReadRequest": {
        "service": "AmazonDynamoDB", "product_family": "Amazon DynamoDB PayPerRequest Throughput",
        "attribute_filters": [{"key": "group", "value": "DDB-ReadUnits"}],
        "purchase_option": "on_demand", "unit": "ReadRequestUnits",
    },
    # Fargate ARM (Graviton) — price per vCPU-hour and GB-hour.
    # The usagetype value encodes the region as a prefix (e.g. CAN1- / USE1-);
    # REGION_PREFIX is resolved at query time from the region map.
    "ECS-Fargate-vCPU-Hour-ARM": {
        "service": "AmazonECS", "product_family": "Compute",
        "attribute_filters": [{"key": "usagetype", "value": "REGION_PREFIX-Fargate-ARM-vCPU-Hours:perCPU"}],
        "unit": "hours",
    },
    "ECS-Fargate-GB-Hour-ARM": {
        "service": "AmazonECS", "product_family": "Compute",
        "attribute_filters": [{"key": "usagetype", "value": "REGION_PREFIX-Fargate-ARM-GB-Hours"}],
        "unit": "hours",
    },
    "ECS-Fargate-Ephemeral-Storage": {
        "service": "AmazonECS", "product_family": "Compute",
        "attribute_filters": [{"key": "usagetype", "value": "REGION_PREFIX-Fargate-EphemeralStorage-GB-Hours"}],
        "unit": "GB-Hours",
    },
    # Application Load Balancer (#352): ALB-hours and used LCU-hours. The group
    # "ELB:Balancing" also holds the Outposts-, TS- and ReservedLCU products, so
    # each descriptor names its usagetype. `store_service` stores the rows under
    # "AmazonALB", the service that the handler and seed use.
    "ALB-Hour": {
        "service": "AWSELB", "store_service": "AmazonALB",
        "product_family": "Load Balancer-Application",
        "attribute_filters": [{"key": "usagetype", "value": "REGION_PREFIX-LoadBalancerUsage"}],
        "unprefixed_in_us_east_1": True,
        "unit": "Hrs",
    },
    "ALB-LCU-ProcessedBytes": {
        "service": "AWSELB", "store_service": "AmazonALB",
        "product_family": "Load Balancer-Application",
        "attribute_filters": [{"key": "usagetype", "value": "REGION_PREFIX-LCUUsage"}],
        "unprefixed_in_us_east_1": True,
        "unit": "LCU-Hrs",
    },
    # NAT Gateway: Infracost prices this under service "AmazonEC2" / productFamily
    # "NAT Gateway" (operation=NatGateway distinguishes it from RegionalNatGateway),
    # but the handler and seed model it under "AmazonVPC" — so store_service remaps
    # it there. Hourly is a single Hrs row; data-processed shares its GB unit with a
    # $0 "Prvd" (provisioned-throughput) row, excluded via usagetype_exclude.
    "NAT-Gateway-Hour": {
        "service": "AmazonEC2", "store_service": "AmazonVPC",
        "product_family": "NAT Gateway",
        "attribute_filters": [{"key": "operation", "value": "NatGateway"}],
        "unit": "Hrs",
    },
    "NAT-Gateway-DataProcessed": {
        "service": "AmazonEC2", "store_service": "AmazonVPC",
        "product_family": "NAT Gateway",
        "attribute_filters": [{"key": "operation", "value": "NatGateway"}],
        "unit": "GB", "usagetype_exclude": ["Prvd"],
    },
    # VPC Interface Endpoint (PrivateLink): ENI-hour + per-GB.
    "VPC-Endpoint-Hour": {
        "service": "AmazonVPC", "product_family": "VpcEndpoint",
        "attribute_filters": [{"key": "endpointType", "value": "PrivateLink"},
                              {"key": "groupDescription", "value": "Hourly charge for VPC Endpoints"}],
        "unit": "Hrs",
    },
    "VPC-Endpoint-DataProcessed": {
        "service": "AmazonVPC", "product_family": "VpcEndpoint",
        "attribute_filters": [{"key": "endpointType", "value": "PrivateLink"},
                              {"key": "groupDescription", "value": "Charge for per GB data processed by VPC Endpoints"}],
        "unit": "GB",
    },
    # CloudWatch Logs (#350): Standard log class ingestion ($/GB) and log
    # storage ($/GB-month). Each usagetype names one product. The group
    # "Ingested Logs" also holds the tiered vended-log products, and the group
    # "Centralized Logs" is cross-account centralization, not storage. In
    # us-east-1 the unprefixed usagetypes are legacy duplicates of the USE1- ones.
    "CloudWatch-Log-Ingestion": {
        "service": "AmazonCloudWatch",
        "attribute_filters": [{"key": "usagetype", "value": "REGION_PREFIX-DataProcessing-Bytes"}],
        "unit": "GB",
    },
    "CloudWatch-Log-Storage": {
        "service": "AmazonCloudWatch",
        "attribute_filters": [{"key": "usagetype", "value": "REGION_PREFIX-TimedStorage-ByteHrs"}],
        "unit": "GB-Mo",
    },
    # Secrets Manager: per-secret per month.
    "SecretsManager-Secret": {
        "service": "AWSSecretsManager", "product_family": "Secret",
        "unit": "Secrets",
    },
    # ECR (#353): standard image storage per GB-month, not archive storage.
    "ECR-Storage": {
        "service": "AmazonECR", "product_family": "EC2 Container Registry",
        "attribute_filters": [{"key": "usagetype", "value": "REGION_PREFIX-TimedStorage-ByteHrs"}],
        "unprefixed_in_us_east_1": True,
        "unit": "GB-Mo",
    },
    # Route 53 (#361): per hosted zone per month, $0.50 for the first 25
    # zones and $0.10 after. The hosted-zone product is in the global
    # catalogue (region ""). The family "DNS Domain Names" is DNS Firewall.
    "Route53-HostedZone": {
        "service": "AmazonRoute53", "product_family": "DNS Zone",
        "query_region": "",
        "attribute_filters": [{"key": "usagetype", "value": "HostedZone"}],
        "unit": "HostedZone", "store_unit": "Zones",
    },
    # Route 53 (#384): standard queries to public hosted zones, $0.40 per
    # million for the first billion a month and $0.20 after. The product is
    # in the global catalogue. Each region's "USE1-DNS-Queries" product is
    # Route 53 Resolver queries. The seed file prices a million queries, so
    # `unit_scale` turns the price per query into a price per million.
    "Route53-Query": {
        "service": "AmazonRoute53", "product_family": "DNS Query",
        "query_region": "",
        "attribute_filters": [{"key": "usagetype", "value": "DNS-Queries"}],
        "unit": "Queries", "unit_scale": 1e-6, "store_unit": "1M-Queries",
    },
    # S3 (#354): AWS bills PUT, COPY, POST and LIST requests as Tier 1 requests.
    "S3-PutRequest": {
        "service": "AmazonS3", "product_family": "API Request",
        "attribute_filters": [{"key": "usagetype", "value": "REGION_PREFIX-Requests-Tier1"}],
        "unprefixed_in_us_east_1": True,
        "unit": "Requests",
    },
    # KMS (#208): $1/customer-managed key-month + per-symmetric-request.
    # Note: the Infracost service code for KMS is lowercase "awskms".
    "KMS-Key-Month": {
        "service": "awskms", "store_service": "AWSKMS",
        "product_family": "Encryption Key",
        "unit": "Keys",
    },
    "KMS-API-Request": {
        "service": "awskms", "store_service": "AWSKMS",
        "product_family": "API Request",
        "attribute_filters": [{"key": "group", "value": "awskms-APIRequest-All"}],
        "unit": "Requests",
    },
    # WAFv2 (#234): web ACL + per-rule monthly + per-request inspection. Infracost
    # prices these under the lowercase service code "awswaf", product family
    # "Web Application Firewall"; `store_service` upserts them under "AWSWAF" so
    # the handler/seed (which use the uppercase convention) can query them — same
    # remap the NAT Gateway descriptor does (EC2 → AmazonVPC).
    # The usagetype encodes the region as a short prefix (USE1- / CAN1- / …),
    # resolved at query time; the "V2" suffix distinguishes WAFv2 from classic
    # WAF, and the exact-match value naturally excludes the "ShieldProtected-"
    # siblings (Infracost matches these with a `(?!ShieldProtected-)` regex).
    # A web ACL with the scope CLOUDFRONT bills from the "Global-" products
    # (#385): `global_scope` syncs them under the region "global", where the
    # WAF handler puts such a web ACL. `store_unit` gives the rows the units
    # of the seed rows.
    # RequestV2-Tier1 is the standard per-request inspection tier. No `unit`
    # filter: each usagetype resolves to a single price row, so filtering by it
    # would only risk a spurious miss on the (region-independent) unit string.
    "WAF-WebACL-Month": {
        "service": "awswaf", "store_service": "AWSWAF",
        "product_family": "Web Application Firewall",
        "attribute_filters": [{"key": "usagetype", "value": "REGION_PREFIX-WebACLV2"}],
        "global_scope": True, "store_unit": "Months",
    },
    "WAF-Rule-Month": {
        "service": "awswaf", "store_service": "AWSWAF",
        "product_family": "Web Application Firewall",
        "attribute_filters": [{"key": "usagetype", "value": "REGION_PREFIX-RuleV2"}],
        "global_scope": True, "store_unit": "Rules",
    },
    "WAF-Request": {
        "service": "awswaf", "store_service": "AWSWAF",
        "product_family": "Web Application Firewall",
        "attribute_filters": [{"key": "usagetype", "value": "REGION_PREFIX-RequestV2-Tier1"}],
        "global_scope": True, "store_unit": "requests",
    },
    # Public IPv4 address (#210): $0.005/hr in-use or idle. The usagetype encodes
    # the region as a short prefix (USE1- / …); REGION_PREFIX is resolved at query
    # time. Product family is unset on these rows, so the usagetype filter alone
    # selects the address (and distinguishes in-use from idle).
    "IPv4-InUse-Hours": {
        "service": "AmazonVPC",
        "attribute_filters": [{"key": "usagetype", "value": "REGION_PREFIX-PublicIPv4:InUseAddress"}],
        "unit": "Hrs",
    },
    "IPv4-Idle-Hours": {
        "service": "AmazonVPC",
        "attribute_filters": [{"key": "usagetype", "value": "REGION_PREFIX-PublicIPv4:IdleAddress"}],
        "unit": "Hrs",
    },
    # CloudWatch Metrics/Alarms (#209). Custom-metric pricing is tiered
    # ($0.30 / $0.10 / $0.05 / $0.02) and comes back as multiple tiers under the
    # one usagetype. GetMetricData is a per-metric API request (excludes the
    # GetMetricWidgetImage rows that share the family) with no free tier.
    # The usagetypes are bare in us-east-1 and prefixed elsewhere (#359).
    "CloudWatch-Metric-Month": {
        "service": "AmazonCloudWatch", "product_family": "Metric",
        "attribute_filters": [{"key": "usagetype", "value": "REGION_PREFIX-CW:MetricMonitorUsage"}],
        "unprefixed_in_us_east_1": True,
        "unit": "Metrics",
    },
    "CloudWatch-Alarm-Month": {
        "service": "AmazonCloudWatch", "product_family": "Alarm",
        "attribute_filters": [{"key": "usagetype", "value": "REGION_PREFIX-CW:AlarmMonitorUsage"}],
        "unprefixed_in_us_east_1": True,
        "unit": "Alarms",
    },
    "CloudWatch-GetMetricData": {
        "service": "AmazonCloudWatch", "product_family": "API Request",
        "attribute_filters": [{"key": "usagetype", "value": "REGION_PREFIX-CW:GMD-Metrics"}],
        "unprefixed_in_us_east_1": True,
        "unit": "Metrics",
    },
    # Inter-region data transfer (#211): priced under service "AWSDataTransfer",
    # catalogued globally (region="") with a distinct usagetype PER source/dest
    # region pair (e.g. USE1-APS4-AWS-Out-Bytes at $0.02/GB, transferType
    # "InterRegion Outbound"). `query_region: ""` queries the global catalogue;
    # `region_pair_source` collapses the pairs leaving the sync region to the
    # modal (standard) rate and stores it under that region. See
    # _upsert_region_pair_representative.
    "DataTransfer-InterRegion-GB": {
        "service": "AWSDataTransfer",
        "query_region": "",
        "attribute_filters": [{"key": "transferType", "value": "InterRegion Outbound"}],
        "unit": "GB",
        "region_pair_source": True,
        "usagetype_suffix": "-AWS-Out-Bytes",
    },
    # Internet egress (#211): transferType "AWS Outbound", one usagetype per
    # source region (us-east-1 is the unprefixed "DataTransfer-Out-Bytes"),
    # tiered $0.09 / $0.085 / $0.07 / $0.05 across the 10/50/150 TB breakpoints.
    "DataTransfer-Internet-Out-GB": {
        "service": "AWSDataTransfer",
        "query_region": "",
        "attribute_filters": [{"key": "transferType", "value": "AWS Outbound"}],
        "unit": "GB",
        "regionless_usagetype": True,
        "usagetype_base": "DataTransfer-Out-Bytes",
    },
    # Regional inter-AZ transfer (#211): transferType "IntraRegion", flat $0.01/GB,
    # usagetype "<region>-DataTransfer-Regional-Bytes" (bare for us-east-1).
    "DataTransfer-InterAZ-GB": {
        "service": "AWSDataTransfer",
        "query_region": "",
        "attribute_filters": [{"key": "transferType", "value": "IntraRegion"}],
        "unit": "GB",
        "regionless_usagetype": True,
        "usagetype_base": "DataTransfer-Regional-Bytes",
    },
    # --- Azure (#226) ------------------------------------------------------------
    # Rows are stored under the service the Azure handler names, since Infracost
    # names the services differently ("Functions", "Azure Cosmos DB", "Storage").
    # Each filter names the product, SKU and meter, which picks one product.
    # Azure prices some meters per block of units ("10", "10K", "1M");
    # `unit_scale` turns the price and the tier bounds into one unit.
    # Functions consumption plan: $0.20 per million executions and $0.000016 per
    # GB-second, after a monthly free 1M executions and 400,000 GB-seconds.
    "AzureFunctions-Execution": {
        "vendor": "azure", "service": "Functions", "store_service": "AzureFunctions",
        "attribute_filters": [{"key": "productName", "value": "Functions"},
                              {"key": "skuName", "value": "Standard"},
                              {"key": "meterName", "value": "Standard Total Executions"}],
        "unit": "10", "unit_scale": 10, "store_unit": "executions",
    },
    "AzureFunctions-GB-Second": {
        "vendor": "azure", "service": "Functions", "store_service": "AzureFunctions",
        "attribute_filters": [{"key": "productName", "value": "Functions"},
                              {"key": "skuName", "value": "Standard"},
                              {"key": "meterName", "value": "Standard Execution Time"}],
        "unit": "1 GB Second", "store_unit": "GB-s",
    },
    # Cosmos DB serverless: $0.25 per million request units, and transactional
    # storage at $0.25 per GB-month (the "RUs" SKU, which serverless accounts use).
    "CosmosDB-Serverless-RU": {
        "vendor": "azure", "service": "Azure Cosmos DB", "store_service": "CosmosDB",
        "attribute_filters": [{"key": "productName", "value": "Azure Cosmos DB serverless"},
                              {"key": "meterName", "value": "1M RUs"}],
        "unit": "1M", "unit_scale": 1_000_000, "store_unit": "RUs",
    },
    "CosmosDB-Storage-GB-Month": {
        "vendor": "azure", "service": "Azure Cosmos DB", "store_service": "CosmosDB",
        "attribute_filters": [{"key": "productName", "value": "Azure Cosmos DB"},
                              {"key": "skuName", "value": "RUs"},
                              {"key": "meterName", "value": "Data Stored"}],
        "unit": "1 GB/Month", "store_unit": "GB-Mo",
    },
    # API Management consumption tier: $3.50 per million calls after a monthly
    # free 1M. The other tiers bill per unit-hour, which no handler metric models.
    "APIM-Consumption-Call": {
        "vendor": "azure", "service": "API Management", "store_service": "APIManagement",
        "attribute_filters": [{"key": "productName", "value": "API Management"},
                              {"key": "skuName", "value": "Consumption"},
                              {"key": "meterName", "value": "Consumption Calls"}],
        "unit": "10K", "unit_scale": 10_000, "store_unit": "requests",
    },
    # Blob Storage, general-purpose v2 block blobs in the Hot tier with LRS, the
    # defaults of `azurerm_storage_account`. Storage is tiered at 50 TB and 500 TB.
    "Blob-Hot-LRS-GB-Month": {
        "vendor": "azure", "service": "Storage", "store_service": "BlobStorage",
        "attribute_filters": [{"key": "productName", "value": "General Block Blob v2"},
                              {"key": "skuName", "value": "Hot LRS"},
                              {"key": "meterName", "value": "Hot LRS Data Stored"}],
        "unit": "1 GB/Month", "store_unit": "GB-Mo",
    },
    "Blob-Hot-Read-Operation": {
        "vendor": "azure", "service": "Storage", "store_service": "BlobStorage",
        "attribute_filters": [{"key": "productName", "value": "General Block Blob v2"},
                              {"key": "skuName", "value": "Hot LRS"},
                              {"key": "meterName", "value": "Hot Read Operations"}],
        "unit": "10K", "unit_scale": 10_000, "store_unit": "requests",
    },
    "Blob-Hot-LRS-Write-Operation": {
        "vendor": "azure", "service": "Storage", "store_service": "BlobStorage",
        "attribute_filters": [{"key": "productName", "value": "General Block Blob v2"},
                              {"key": "skuName", "value": "Hot LRS"},
                              {"key": "meterName", "value": "Hot LRS Write Operations"}],
        "unit": "10K", "unit_scale": 10_000, "store_unit": "requests",
    },
    # Internet egress (#372). Azure bills every service's egress on one
    # Bandwidth meter, so the handlers price `dataOutGb` under "Bandwidth"
    # (`catalog_services`). The default routing is the Microsoft global
    # network ("Rtn Preference: MGN"). The first 100 GB a month are free.
    # Infracost gives this meter with the tier starts of an older price list
    # (5, 10240 GB ...) beside the current ones (100, 10335 GB ...), so the
    # sync reads the Azure Retail Prices API (`azure_retail`).
    "Bandwidth-Internet-Out-GB": {
        "vendor": "azure", "service": "Bandwidth", "store_service": "Bandwidth",
        "azure_retail": True,
        "attribute_filters": [{"key": "productName", "value": "Rtn Preference: MGN"},
                              {"key": "skuName", "value": "Standard"},
                              {"key": "meterName", "value": "Standard Data Transfer Out"}],
        "unit": "1 GB", "store_unit": "GB",
    },
    # --- GCP (#226) --------------------------------------------------------------
    # GCP rows have two attributes, a description and a resource group. Some
    # products are in Infracost's global catalogue (`query_region: "global"`) and
    # are stored under the sync region. Tier 2 regions name the tier in the
    # description, so `attribute_patterns` matches both spellings.
    # Cloud Run functions (1st gen): invocations, memory GB-seconds and CPU
    # GHz-seconds. The first 2M invocations a month are free.
    "CloudFunctions-Invocation": {
        "vendor": "gcp", "service": "Cloud Run Functions", "store_service": "CloudFunctions",
        "query_region": "global",
        "attribute_filters": [{"key": "description",
                               "value": "Cloud Run Functions (1st Gen) Invocations"}],
        "unit": "count",
    },
    "CloudFunctions-GB-Second": {
        "vendor": "gcp", "service": "Cloud Run Functions", "store_service": "CloudFunctions",
        "attribute_filters": [{"key": "resourceGroup", "value": "Functions"}],
        "attribute_patterns": {"description": r"Cloud Run functions \(1st Gen\) Memory"
                                              r"( Tier 2)? +\(Request-based billing\)"},
        "unit": "gibibyte second",
    },
    "CloudFunctions-GHz-Second": {
        "vendor": "gcp", "service": "Cloud Run Functions", "store_service": "CloudFunctions",
        "attribute_filters": [{"key": "resourceGroup", "value": "Functions"}],
        "attribute_patterns": {"description": r"Cloud Run functions \(1st Gen\) CPU"
                                              r"( Tier 2)? +\(Request-based billing\)"},
        "unit": "second",
    },
    # Cloud Run services with request-based billing. The first 2M requests a
    # month are free. `CLOUD_RUN_TIER` selects the region's price tier (#376).
    "CloudRun-Request": {
        "vendor": "gcp", "service": "Cloud Run", "store_service": "CloudRun",
        "query_region": "global",
        "attribute_filters": [{"key": "description", "value": "Requests"}],
        "unit": "count",
    },
    "CloudRun-vCPU-Second": {
        "vendor": "gcp", "service": "Cloud Run", "store_service": "CloudRun",
        "attribute_filters": [{"key": "resourceGroup", "value": "Compute"}],
        "attribute_patterns": {"description": r"Services CPUCLOUD_RUN_TIER +\(Request-based billing\)"},
        "unit": "second",
    },
    "CloudRun-GiB-Second": {
        "vendor": "gcp", "service": "Cloud Run", "store_service": "CloudRun",
        "attribute_filters": [{"key": "resourceGroup", "value": "Compute"}],
        "attribute_patterns": {"description": r"Services MemoryCLOUD_RUN_TIER +\(Request-based billing\)"},
        "unit": "gibibyte second",
    },
    # Internet egress (#372). GCP bills it under each service's own SKUs, and
    # counts the monthly tiers for each SKU, so each service has its own
    # metric. Each region has one product for traffic to its own continent
    # ("North America to North America"), which the pattern selects. The North
    # America product starts with the free 1 GiB a month.
    "CloudRun-Internet-Egress-GiB": {
        "vendor": "gcp", "service": "Cloud Run", "store_service": "CloudRun",
        "attribute_filters": [{"key": "resourceGroup", "value": "PremiumInternetEgress"}],
        "attribute_patterns": {"description": r"Cloud Run Network Internet Data Transfer"
                                              r" Out (.+) to \1"},
        "unit": "gibibyte",
    },
    # Cloud Storage, Standard class in a single region. Storage is in the
    # regional catalogue. Class A (writes, lists) and Class B (reads) operations
    # are global, with 5,000 and 50,000 free a month.
    "GCS-Standard-GiB-Month": {
        "vendor": "gcp", "service": "Cloud Storage", "store_service": "CloudStorage",
        "attribute_filters": [{"key": "resourceGroup", "value": "RegionalStorage"}],
        "unit": "gibibyte month",
    },
    "GCS-Class-A-Operation": {
        "vendor": "gcp", "service": "Cloud Storage", "store_service": "CloudStorage",
        "query_region": "global",
        "attribute_filters": [{"key": "description",
                               "value": "Regional Standard Class A Operations"}],
        "unit": "count",
    },
    "GCS-Class-B-Operation": {
        "vendor": "gcp", "service": "Cloud Storage", "store_service": "CloudStorage",
        "query_region": "global",
        "attribute_filters": [{"key": "description",
                               "value": "Regional Standard Class B Operations"}],
        "unit": "count",
    },
    # Internet egress (#372) to worldwide destinations other than Asia and
    # Australia, in the global catalogue. The product starts with the 100 GiB
    # a month of Always Free egress, which applies in us-central1, us-east1
    # and us-west1 only (`FREE_ALLOWANCE_REGIONS`).
    "GCS-Internet-Egress-GiB": {
        "vendor": "gcp", "service": "Cloud Storage", "store_service": "CloudStorage",
        "query_region": "global",
        "attribute_filters": [{"key": "description", "value":
                               "Download Worldwide Destinations (excluding Asia & Australia)"}],
        "unit": "gibibyte",
    },
    # Firestore Standard edition, in the global catalogue with one product for
    # each location. GCP_LOCATION is resolved from the sync region. These are
    # the products without the free quota: the "(with free tier)" products
    # state the daily quota as a tier, and catalog tiers count a month. The
    # sync adds the quota as a month of days (`FREE_ALLOWANCES`, #373).
    "Firestore-Read": {
        "vendor": "gcp", "service": "Cloud Firestore", "store_service": "Firestore",
        "query_region": "global",
        "attribute_filters": [{"key": "description",
                               "value": "Cloud Firestore Read Ops GCP_LOCATION"}],
        "unit": "count",
    },
    "Firestore-Write": {
        "vendor": "gcp", "service": "Cloud Firestore", "store_service": "Firestore",
        "query_region": "global",
        "attribute_filters": [{"key": "description",
                               "value": "Cloud Firestore Entity Writes GCP_LOCATION"}],
        "unit": "count",
    },
    "Firestore-GiB-Month": {
        "vendor": "gcp", "service": "Cloud Firestore", "store_service": "Firestore",
        "query_region": "global",
        "attribute_filters": [{"key": "description",
                               "value": "Cloud Firestore Storage GCP_LOCATION"}],
        "unit": "gibibyte month",
    },
}


def _live_auth_intended(client: "InfracostClient") -> bool:
    """Whether the caller intended a live sync (a credential is present)."""
    return client.is_authenticated()


def sync_pricing_catalog(vendor: str = "aws", services: list[str] = None,
                         fallback: bool = False,
                         regions: list[str] = None) -> tuple[int, str]:
    """Sync pricing into the cache, live from Infracost when authenticated.

    Fetches every descriptor's prices for each requested region (defaults to
    us-east-1 for backward compatibility; the CLI ``sync-pricing`` command passes
    the full region set so pricing covers all services and all regions). Falls
    back to the bundled seed price list when there is no credential, but emits a
    ``UserWarning`` when a credential WAS present and the live sync failed — so a
    broken live path is never silently mistaken for success.
    """
    from infra_cost_model.pricing.cache import PricingCache

    cache = PricingCache()

    if fallback:
        return _sync_fallback(vendor, services, cache)

    client = InfracostClient()
    if not client.is_authenticated():
        # No credential — seed fallback is expected, not an error.
        return _sync_fallback(vendor, services, cache)

    metrics = services if services else list(METRIC_DESCRIPTORS.keys())
    if not regions:
        regions = ["us-east-1"]
    total = 0
    failures: list[str] = []
    for region in regions:
        for metric in metrics:
            if metric not in METRIC_DESCRIPTORS:
                continue
            if METRIC_DESCRIPTORS[metric].get("vendor", "aws") != vendor:
                continue
            if region == GLOBAL_REGION and not METRIC_DESCRIPTORS[metric].get("global_scope"):
                continue
            try:
                total += client.sync_to_cache(cache, metric, region, vendor)
            except (RuntimeError, requests.RequestException, KeyError) as exc:
                failures.append(f"{region}/{metric}: {exc}")

    if total == 0:
        warnings.warn(
            "Infracost credential present but the live pricing sync returned no "
            f"rows; falling back to the seed price list. Failures: {failures}",
            UserWarning,
        )
        return _sync_fallback(vendor, services, cache)
    if failures:
        warnings.warn(
            f"Infracost live sync partially failed ({len(failures)} metric(s)): "
            f"{failures}",
            UserWarning,
        )
    return total, "infracost"


def seed_pricing_catalog(services: list[str] | None = None) -> tuple[int, str]:
    """Seed the pricing catalog from the bundled seed file (offline).

    With ``services=None``, loads every row of the seed file, as
    ``PricingCatalog(seed=True)`` does. With a list, loads the seed rows of
    those AWS services only. See ``aws_fallback_prices``.
    """
    from infra_cost_model.pricing.cache import PricingCache
    from .aws_pricing import aws_fallback_prices

    count = aws_fallback_prices(services, PricingCache(), seed_only=True)
    return count, "seed-pricelist"


def _sync_fallback(vendor: str, services: list[str] | None, cache) -> tuple[int, str]:
    """Load prices without Infracost: the seed file, then the AWS Price List API.

    With ``services=None``, loads every service in the seed file, as
    ``seed-pricing`` does, then fetches live prices for the services in
    ``SERVICE_CODES`` that the seed file didn't cover. With a list, loads the
    seed rows of those services, and skips with a warning any name that the
    live fetch can't look up. See ``aws_fallback_prices``.
    """
    from .aws_pricing import aws_fallback_prices

    if vendor != "aws":
        return 0, "fallback-unsupported"
    count = aws_fallback_prices(services, cache)
    return count, "aws-pricelist"
