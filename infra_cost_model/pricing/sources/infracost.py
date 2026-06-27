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

        prices = self.query_prices(
            service=descriptor["service"],
            region=region,
            product_family=descriptor.get("product_family"),
            attribute_filters=descriptor.get("attribute_filters"),
            purchase_option=descriptor.get("purchase_option"),
            vendor=vendor,
        )
        unit_match = descriptor.get("unit")
        now = datetime.now().isoformat()
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
