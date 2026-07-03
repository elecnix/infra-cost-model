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

import os
import json
import platform
import warnings
import requests
from pathlib import Path
from datetime import datetime
from typing import Optional

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
    "ap-southeast-1": "APS1", "ap-southeast-2": "APS2", "ap-southeast-3": "APS3",
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
        """Fetch the prices for one catalog usage_metric and upsert them.

        Resolves the metric to an Infracost product descriptor (service, family,
        attribute filters, purchase option, unit) and stores the matching prices
        under the catalog's ``usage_metric`` name.
        """
        from infra_cost_model.pricing.cache import Price

        descriptor = METRIC_DESCRIPTORS.get(usage_metric)
        if descriptor is None:
            raise KeyError(f"No Infracost descriptor for usage_metric '{usage_metric}'")

        # Some services (notably AWSDataTransfer) catalogue their products
        # globally, with region="". `query_region` lets a descriptor query that
        # global catalogue while the price is still stored under the caller's
        # `region` (see `region_pair_source` below).
        query_region = descriptor.get("query_region", region)
        prices = self.query_prices(
            service=descriptor["service"],
            region=query_region,
            product_family=descriptor.get("product_family"),
            attribute_filters=descriptor.get("attribute_filters"),
            purchase_option=descriptor.get("purchase_option"),
            vendor=vendor,
        )
        unit_match = descriptor.get("unit")
        now = datetime.now().isoformat()

        if descriptor.get("region_pair_source"):
            return self._upsert_region_pair_representative(
                cache, prices, usage_metric, region, unit_match, descriptor, now)

        if descriptor.get("regionless_usagetype"):
            return self._upsert_regionless_usagetype(
                cache, prices, usage_metric, region, unit_match, descriptor, now)

        count = 0
        for p in prices:
            if unit_match and p.get("unit") != unit_match:
                continue
            cache.upsert(Price(
                vendor=p["vendor"], service=p["service"], region=p["region"],
                product_family=p["product_family"], attributes=p["attributes"],
                usage_metric=usage_metric, unit=p["unit"], price_usd=p["price_usd"],
                start_usage_amount=p["start_usage_amount"],
                end_usage_amount=p["end_usage_amount"],
                source="infracost", effective_date=now, fetched_at=now,
            ))
            count += 1
        return count

    def _upsert_region_pair_representative(self, cache, prices, usage_metric,
                                           region, unit_match, descriptor, now) -> int:
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

        Returns the number of rows upserted (0 or 1).
        """
        from infra_cost_model.pricing.cache import Price
        from collections import Counter

        prefix = _region_usagetype_prefix(region)
        suffix = descriptor.get("usagetype_suffix", "-AWS-Out-Bytes")
        candidates = []
        for p in prices:
            if unit_match and p.get("unit") != unit_match:
                continue
            usagetype = (p.get("attributes") or {}).get("usagetype", "")
            if not usagetype.startswith(f"{prefix}-") or not usagetype.endswith(suffix):
                continue
            if (p.get("price_usd") or 0) <= 0:
                continue
            candidates.append(p)

        if not candidates:
            return 0

        # Modal price = the region's standard rate; tie-break toward the lower rate.
        counts = Counter(round(p["price_usd"], 6) for p in candidates)
        modal = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]
        rep = next(p for p in candidates if round(p["price_usd"], 6) == modal)

        cache.upsert(Price(
            vendor=rep["vendor"], service=rep["service"], region=region,
            product_family=rep.get("product_family"), attributes=rep.get("attributes", {}),
            usage_metric=usage_metric, unit=rep["unit"], price_usd=rep["price_usd"],
            start_usage_amount=None, end_usage_amount=None,
            source="infracost", effective_date=now, fetched_at=now,
        ))
        return 1

    def _upsert_regionless_usagetype(self, cache, prices, usage_metric,
                                     region, unit_match, descriptor, now) -> int:
        """Store a globally-catalogued, single-usagetype metric under the region.

        Unlike inter-region transfer, internet egress and inter-AZ transfer have
        ONE usagetype per source region (not per region pair), but still live in
        the global (region="") ``AWSDataTransfer`` catalogue. This keeps every row
        for the region's usagetype — preserving tiers (internet egress is tiered
        $0.09 / $0.085 / $0.07 / $0.05) — and stores them under the sync ``region``.

        us-east-1 data-transfer usagetypes are unprefixed (an AWS legacy quirk);
        every other region prepends its short prefix (e.g. ``USW1-``).

        Returns the number of rows upserted.
        """
        from infra_cost_model.pricing.cache import Price

        base = descriptor["usagetype_base"]
        prefix = _region_usagetype_prefix(region)
        target = base if region == "us-east-1" else f"{prefix}-{base}"

        count = 0
        for p in prices:
            if unit_match and p.get("unit") != unit_match:
                continue
            if (p.get("attributes") or {}).get("usagetype") != target:
                continue
            cache.upsert(Price(
                vendor=p["vendor"], service=p["service"], region=region,
                product_family=p.get("product_family"), attributes=p.get("attributes", {}),
                usage_metric=usage_metric, unit=p["unit"], price_usd=p["price_usd"],
                start_usage_amount=p["start_usage_amount"],
                end_usage_amount=p["end_usage_amount"],
                source="infracost", effective_date=now, fetched_at=now,
            ))
            count += 1
        return count


# Map each catalog usage_metric to the Infracost product query that prices it.
# Validated against the live Cloud Pricing API; extend per service as needed.
METRIC_DESCRIPTORS: dict[str, dict] = {
    "Lambda-Request": {
        "service": "AWSLambda", "product_family": "Serverless",
        "attribute_filters": [{"key": "group", "value": "AWS-Lambda-Requests"}],
        "purchase_option": "on_demand", "unit": "Requests",
    },
    "Lambda-GB-Second": {
        "service": "AWSLambda", "product_family": "Serverless",
        "attribute_filters": [{"key": "group", "value": "AWS-Lambda-Duration"}],
        "purchase_option": "on_demand", "unit": "seconds",
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
    # Application Load Balancer: ALB-hours (resource type ELB:Balancing) + LCU.
    "ALB-Hour": {
        "service": "AWSELB", "product_family": "Load Balancer-Application",
        "attribute_filters": [{"key": "group", "value": "ELB:Balancing"}],
        "unit": "Hrs",
    },
    "ALB-LCU-ProcessedBytes": {
        "service": "AWSELB", "product_family": "Load Balancer-Application",
        "attribute_filters": [{"key": "group", "value": "ELB:Balancing"}],
        "unit": "LCU-Hrs",
    },
    # NAT Gateway: the Infracost catalog doesn't currently expose NAT GW under a
    # standard productFamily, so these entries are present but not yet live-validated.
    # The infracost CLI does price this resource; the product grouping is TBD.
    #"NAT-Gateway-Hour": { "service": "AmazonVPC" },
    #"NAT-Gateway-DataProcessed": { "service": "AmazonVPC" },
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
    # CloudWatch Logs: ingestion ($/GB) + storage ($/GB-month).
    "CloudWatch-Log-Ingestion": {
        "service": "AmazonCloudWatch",
        "attribute_filters": [{"key": "group", "value": "Ingested Logs"}],
        "unit": "GB",
    },
    "CloudWatch-Log-Storage": {
        "service": "AmazonCloudWatch",
        "attribute_filters": [{"key": "group", "value": "Centralized Logs"}],
        "unit": "GB",
    },
    # Secrets Manager: per-secret per month.
    "SecretsManager-Secret": {
        "service": "AWSSecretsManager", "product_family": "Secret",
        "unit": "Secrets",
    },
    # ECR: image storage per GB-month.
    "ECR-Storage": {
        "service": "AmazonECR", "product_family": "EC2 Container Registry",
        "attribute_filters": [{"key": "groupDescription", "value": ""}],
        "unit": "GB-Mo",
    },
    # Route53: per hosted zone per month.
    "Route53-HostedZone": {
        "service": "AmazonRoute53", "product_family": "DNS Domain Names",
        "unit": "Mo",
    },
    # S3: PUT requests.
    "S3-PutRequest": {
        "service": "AmazonS3", "product_family": "API Request",
        "attribute_filters": [{"key": "group", "value": "S3-API-PutObject"}],
        "unit": "Requests",
    },
    # KMS (#208): $1/customer-managed key-month + per-symmetric-request.
    # Note: the Infracost service code for KMS is lowercase "awskms".
    "KMS-Key-Month": {
        "service": "awskms", "product_family": "Encryption Key",
        "unit": "Keys",
    },
    "KMS-API-Request": {
        "service": "awskms", "product_family": "API Request",
        "attribute_filters": [{"key": "group", "value": "awskms-APIRequest-All"}],
        "unit": "Requests",
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
    # GetMetricWidgetImage rows that share the family) with a 1M free tier.
    "CloudWatch-Metric-Month": {
        "service": "AmazonCloudWatch", "product_family": "Metric",
        "attribute_filters": [{"key": "usagetype", "value": "CW:MetricMonitorUsage"}],
        "unit": "Metrics",
    },
    "CloudWatch-Alarm-Month": {
        "service": "AmazonCloudWatch", "product_family": "Alarm",
        "attribute_filters": [{"key": "usagetype", "value": "CW:AlarmMonitorUsage"}],
        "unit": "Alarms",
    },
    "CloudWatch-GetMetricData": {
        "service": "AmazonCloudWatch", "product_family": "API Request",
        "attribute_filters": [{"key": "usagetype", "value": "CW:GMD-Metrics"}],
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
}


def _live_auth_intended(client: "InfracostClient") -> bool:
    """Whether the caller intended a live sync (a credential is present)."""
    return client.is_authenticated()


def sync_pricing_catalog(vendor: str = "aws", services: list[str] = None,
                         fallback: bool = False) -> tuple[int, str]:
    """Sync pricing into the cache, live from Infracost when authenticated.

    Falls back to the bundled seed price list when there is no credential, but
    emits a ``UserWarning`` when a credential WAS present and the live sync failed
    — so a broken live path is never silently mistaken for success.
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
    region = "us-east-1"
    total = 0
    failures: list[str] = []
    for metric in metrics:
        if metric not in METRIC_DESCRIPTORS:
            continue
        try:
            total += client.sync_to_cache(cache, metric, region, vendor)
        except (RuntimeError, requests.RequestException, KeyError) as exc:
            failures.append(f"{metric}: {exc}")

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


def seed_pricing_catalog(services: list[str] = None) -> tuple[int, str]:
    """Seed the pricing catalog from the bundled seed file (offline)."""
    from infra_cost_model.pricing.cache import PricingCache
    from .aws_pricing import aws_fallback_prices

    cache = PricingCache()
    if services is None:
        services = ["AWSLambda", "AmazonDynamoDB", "AmazonAPIGatewayHTTP", "AmazonBedrock"]
    count = aws_fallback_prices(services, cache, seed_only=True)
    return count, "seed-pricelist"


def _sync_fallback(vendor: str, services: list[str], cache) -> tuple[int, str]:
    from .aws_pricing import aws_fallback_prices

    if vendor != "aws":
        return 0, "fallback-unsupported"
    if services is None:
        services = ["AWSLambda", "AmazonDynamoDB", "AmazonAPIGatewayHTTP", "AmazonBedrock"]
    count = aws_fallback_prices(services, cache)
    return count, "aws-pricelist"
