"""AWS Pricing API client for fallback pricing."""

import warnings
from datetime import datetime

import requests

from infra_cost_model.pricing import cache as cache_module

AWS_PRICE_LIST_URL = "https://pricing.us-east-1.amazonaws.com"

SERVICE_CODES = {
    "AWSLambda": "AWSLambda",
    "AmazonDynamoDB": "AmazonDynamoDB",
    "AmazonAPIGatewayHTTP": "AmazonAPIGatewayV2",
    "AmazonBedrock": "AmazonBedrock",
}

REQUIRED_METRICS = {
    "AWSLambda": ["Lambda-Request", "Lambda-GB-Second"],
    "AmazonDynamoDB": ["Dynamo-ReadRequest", "Dynamo-WriteRequest", "Dynamo-Storage"],
    "AmazonAPIGatewayHTTP": ["APIGateway-HTTP-Request"],
    "AmazonBedrock": ["Bedrock-Input-Token", "Bedrock-Output-Token"],
}


def fetch_aws_price_list(service_code: str) -> list[dict]:
    """Fetch pricing from the public AWS Price List offer file."""
    url = f"{AWS_PRICE_LIST_URL}/offers/v1.0/aws/{service_code}/current/index.json"

    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        return []

    products = data.get("products", {})
    terms = data.get("terms", {}).get("OnDemand", {})
    results = []

    for sku, product in products.items():
        attributes = product.get("attributes", {})
        for dimension in terms.get(sku, {}).get("priceDimensions", {}).values():
            results.append({
                "sku": sku,
                "service": service_code,
                "attributes": attributes,
                "unit": dimension.get("unit", ""),
                "price_usd": _price_usd(dimension),
            })

    return results


def aws_fallback_prices(services: list[str] | None, cache, region: str = "us-east-1",
                        seed_only: bool = False) -> int:
    """Load seed file prices into the cache, then fill gaps from the AWS Price List API.

    Both paths read the seed file through load_seed_rows in
    infra_cost_model.pricing.cache, so they keep every field of a row and
    label it with the source "seed".

    The live fetch covers only services with an entry in SERVICE_CODES. It
    skips any other service name with a UserWarning that names it, and never
    sends that name to the API.

    Args:
        services: AWS service names to sync. None means every service in the
            seed file, loaded through seed_prices, then a live fetch for each
            SERVICE_CODES service that still has no cached rows.
        cache: PricingCache instance
        region: AWS region (default: us-east-1)
        seed_only: If True, only use seed file (don't query API)

    Returns:
        Number of prices synced to cache

    Raises:
        ValueError: If services is an empty list
        RuntimeError: If seed_only is True and the seed file is missing, or if
            no pricing data could be loaded. The message says whether the seed
            file is missing or has no rows for the requested services.
    """
    if services is not None and not services:
        raise ValueError("services must name at least one AWS service, or be None for all")

    try:
        if services is None:
            count = cache_module.seed_prices(cache)
        else:
            count, cached = _load_seed_services(services, cache, region)
        seed_missing = False
    except cache_module.SeedFileNotFound:
        if seed_only:
            raise _no_pricing_error(services, region, seed_missing=True) from None
        seed_missing = True
        count = 0
        cached = _cached_metrics(cache, services, region) if services else {}

    if services is None:
        if seed_only:
            return count
        # Fetch live prices only for known services the seed file didn't cover.
        cached_services = _cached_services(cache, list(SERVICE_CODES), region)
        missing = [s for s in SERVICE_CODES if s not in cached_services]
        count += _fetch_live(missing, cache, region, datetime.now().isoformat())
        if count == 0:
            raise _no_pricing_error(services, region, seed_missing)
        return count

    # If we loaded from seed, return count
    if seed_only or count > 0:
        return count or sum(cached.values())

    # Fetch from the API only the services the cache has no rows for.
    cached_services = {svc for svc, _, _ in cached}
    if cached_services and cached_services >= set(services):
        return sum(cached.values())  # Already seeded, nothing to do
    missing = [s for s in services if s not in cached_services]

    unknown = [s for s in missing if s not in SERVICE_CODES]
    if unknown:
        warnings.warn(
            f"Skipping the AWS Price List fetch for {', '.join(unknown)}: "
            f"no entry in SERVICE_CODES. Known services: {', '.join(SERVICE_CODES)}.",
            UserWarning,
            stacklevel=2,
        )
    known = [s for s in missing if s in SERVICE_CODES]
    count += _fetch_live(known, cache, region, datetime.now().isoformat())

    if count == 0:
        raise _no_pricing_error(services, region, seed_missing)

    return count


def _load_seed_services(services: list[str], cache, region: str) -> tuple[int, dict]:
    """Load the AWS seed rows of these services that the cache lacks.

    Returns the number of rows loaded and the cached row counts per
    (service, region, usage metric) from before the load.

    Raises:
        SeedFileNotFound: If the seed file doesn't exist.
    """
    rows = cache_module.load_seed_rows(services)

    # Skip seed rows the cache already has for the same service, region and
    # usage metric (e.g., seed_prices loaded them already). Prevents duplicate
    # tiered entries from two code paths reading the same JSON file. The check
    # runs per metric, so a cached row for one metric, or a "global" row,
    # doesn't stop the other seed rows of that service from loading.
    cached = _cached_metrics(cache, services, region)

    count = 0
    for price in rows:
        if price.vendor != "aws":
            continue
        # Global services such as CloudFront have no AWS region, so their
        # seed rows use "global" and load for any region.
        if price.region not in (region, "global"):
            continue
        if (price.service, price.region, price.usage_metric) in cached:
            continue
        cache.upsert(price)
        count += 1
    return count, cached


def _cached_metrics(cache, services: list[str], region: str) -> dict:
    """Count the cached AWS rows per (service, region, usage metric)."""
    import sqlite3

    conn = sqlite3.connect(cache.db_path)
    placeholders = ','.join(['?'] * len(services))
    rows = conn.execute(
        f"SELECT service, region, usage_metric, COUNT(*) FROM prices "
        f"WHERE vendor='aws' AND region IN (?, 'global') "
        f"AND service IN ({placeholders}) "
        f"GROUP BY service, region, usage_metric",
        [region] + list(services)
    ).fetchall()
    conn.close()
    return {(svc, reg, metric): n for svc, reg, metric, n in rows}


def _cached_services(cache, services: list[str], region: str) -> set[str]:
    return {svc for svc, _, _ in _cached_metrics(cache, services, region)}


def _fetch_live(services: list[str], cache, region: str, now: str) -> int:
    """Fetch each SERVICE_CODES service from the AWS Price List API into the cache."""
    from infra_cost_model.pricing.cache import Price

    count = 0
    seen: set = set()
    for service in services:
        for item in fetch_aws_price_list(SERVICE_CODES[service]):
            usage_metric = _usage_metric(service, item["attributes"], item["unit"])
            if not usage_metric or item.get("attributes", {}).get("regionCode") != region:
                continue

            key = (service, usage_metric, item["unit"], item["price_usd"])
            if key in seen:
                continue
            seen.add(key)

            cache.upsert(Price(
                vendor="aws",
                service=service,
                region=region,
                product_family=item.get("attributes", {}).get("productFamily", ""),
                attributes=item.get("attributes", {}),
                usage_metric=usage_metric,
                unit=item.get("unit"),
                price_usd=item.get("price_usd", 0),
                source="aws-pricelist",
                effective_date=now,
                fetched_at=now,
            ))
            count += 1
    return count


def _no_pricing_error(services: list[str] | None, region: str,
                      seed_missing: bool) -> RuntimeError:
    """Say why no prices loaded: the seed file is missing, or lacks the services."""
    path = cache_module.SEED_PRICES_PATH
    if seed_missing:
        reason = f"Seed file not found at {path}."
    elif services is None:
        reason = f"The seed file at {path} has no rows, and the live fetch found none."
    else:
        reason = (f"The seed file at {path} has no rows for {', '.join(services)} "
                  f"in {region}, and the live fetch found none.")
    return RuntimeError(
        f"No pricing data available. {reason} "
        f"Run 'infra-cost-model seed-pricing' first, or set INFRACOST_API_KEY for live pricing."
    )


def _price_usd(dimension: dict) -> float:
    try:
        return float(dimension.get("pricePerUnit", {}).get("USD", 0))
    except (TypeError, ValueError):
        return 0.0


def _usage_metric(service: str, attributes: dict, unit: str) -> str | None:
    haystack = " ".join(
        str(attributes.get(key, "")).lower()
        for key in ("operation", "usagetype", "usageType", "productFamily")
    )
    unit = unit.lower()

    if service == "AWSLambda":
        if "request" in haystack or "request" in unit:
            return "Lambda-Request"
        if "gb-second" in haystack or "gb second" in haystack or "gb-seconds" in unit:
            return "Lambda-GB-Second"
    elif service == "AmazonDynamoDB":
        if "readrequest" in haystack or "read request" in haystack:
            return "Dynamo-ReadRequest"
        if "writerequest" in haystack or "write request" in haystack:
            return "Dynamo-WriteRequest"
        if "storage" in haystack:
            return "Dynamo-Storage"
    elif service == "AmazonAPIGatewayHTTP":
        if "http" in haystack or "api request" in haystack:
            return "APIGateway-HTTP-Request"
    elif service == "AmazonBedrock":
        if "input" in haystack or "prompt" in haystack:
            return "Bedrock-Input-Token"
        if "output" in haystack or "completion" in haystack:
            return "Bedrock-Output-Token"

    return None


def _product_family(service: str, metric: str) -> str:
    if service == "AWSLambda":
        return "Serverless"
    if service == "AmazonDynamoDB" and metric == "Dynamo-Storage":
        return "Storage"
    if service == "AmazonDynamoDB":
        return "OnDemand"
    if service == "AmazonAPIGatewayHTTP":
        return "APIGateway"
    return "LLM"
