"""AWS Pricing API client for fallback pricing."""

from datetime import datetime
from pathlib import Path

import requests

AWS_PRICE_LIST_URL = "https://pricing.us-east-1.amazonaws.com"
SEED_PRICES_PATH = Path(__file__).parent.parent.parent.parent / "data" / "seed" / "aws_pricelist_seed.json"

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


def aws_fallback_prices(services: list[str], cache, region: str = "us-east-1", seed_only: bool = False) -> int:
    """Seed pricing cache from AWS Price List API, filling gaps with seed file prices.
    
    Args:
        services: List of AWS service names to sync
        cache: PricingCache instance
        region: AWS region (default: us-east-1)
        seed_only: If True, only use seed file (don't query API)
        
    Returns:
        Number of prices synced to cache
        
    Raises:
        RuntimeError: If no pricing data could be fetched and seed file unavailable
    """
    from infra_cost_model.pricing.cache import Price
    import json

    count = 0
    now = datetime.now().isoformat()
    seen = set()

    # First, load from seed file if it exists
    if SEED_PRICES_PATH.exists():
        try:
            seed_data = json.loads(SEED_PRICES_PATH.read_text())
            for item in seed_data:
                if item.get("vendor") != "aws":
                    continue
                if services and item.get("service") not in services:
                    continue
                if item.get("region") != region:
                    continue
                    
                key = (item["service"], item["usage_metric"], item["unit"], item["price_usd"])
                if key in seen:
                    continue
                seen.add(key)

                cache.upsert(Price(
                    vendor=item["vendor"],
                    service=item["service"],
                    region=item["region"],
                    product_family=item.get("product_family", ""),
                    attributes={},
                    usage_metric=item["usage_metric"],
                    unit=item["unit"],
                    price_usd=item["price_usd"],
                    source="seed-initial",
                    effective_date=now,
                    fetched_at=now,
                ))
                count += 1
        except (json.JSONDecodeError, KeyError):
            pass  # Fall through to API or error
    
    # If we loaded from seed, return count
    if seed_only or count > 0:
        return count

    for service in services:
        synced_metrics = set()
        aws_code = SERVICE_CODES.get(service, service)

        for item in fetch_aws_price_list(aws_code):
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
            synced_metrics.add(usage_metric)
            count += 1

    if count == 0:
        raise RuntimeError(
            f"No pricing data available. Seed file not found at {SEED_PRICES_PATH}. "
            f"Run 'infra-cost-model seed-pricing' first, or set INFRACOST_API_KEY for live pricing."
        )

    return count


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
