"""AWS Pricing API client for fallback pricing."""

from datetime import datetime

import requests

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

STATIC_PRICES = {
    "AWSLambda": {
        "Lambda-Request": [
            {"price_usd": 0.0, "unit": "requests", "start_usage_amount": 0, "end_usage_amount": 1_000_000},
            {"price_usd": 0.20e-6, "unit": "requests", "start_usage_amount": 1_000_000, "end_usage_amount": None},
        ],
        "Lambda-GB-Second": [
            {"price_usd": 0.0000166667, "unit": "GB-s", "start_usage_amount": 0, "end_usage_amount": 6_000_000_000},
        ],
    },
    "AmazonDynamoDB": {
        "Dynamo-ReadRequest": [
            {"price_usd": 1.25e-6, "unit": "requests", "start_usage_amount": 0, "end_usage_amount": None},
        ],
        "Dynamo-WriteRequest": [
            {"price_usd": 6.25e-6, "unit": "requests", "start_usage_amount": 0, "end_usage_amount": None},
        ],
        "Dynamo-Storage": [
            {"price_usd": 0.25, "unit": "GB-Mo", "start_usage_amount": 0, "end_usage_amount": None},
        ],
    },
    "AmazonAPIGatewayHTTP": {
        "APIGateway-HTTP-Request": [
            {"price_usd": 1.00e-6, "unit": "requests", "start_usage_amount": 0, "end_usage_amount": None},
        ],
    },
    "AmazonBedrock": {
        "Bedrock-Input-Token": [
            {"price_usd": 0.003 / 1000, "unit": "tokens", "start_usage_amount": 0, "end_usage_amount": None},
        ],
        "Bedrock-Output-Token": [
            {"price_usd": 0.015 / 1000, "unit": "tokens", "start_usage_amount": 0, "end_usage_amount": None},
        ],
    },
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


def aws_fallback_prices(services: list[str], cache, region: str = "us-east-1") -> int:
    """Seed pricing cache from AWS Price List API, filling gaps with known fallback prices."""
    from infra_cost_model.pricing.cache import Price

    count = 0
    now = datetime.now().isoformat()
    seen = set()

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

        for metric in set(REQUIRED_METRICS.get(service, [])) - synced_metrics:
            for price in STATIC_PRICES.get(service, {}).get(metric, []):
                cache.upsert(Price(
                    vendor="aws",
                    service=service,
                    region=region,
                    product_family=_product_family(service, metric),
                    attributes={},
                    usage_metric=metric,
                    unit=price["unit"],
                    price_usd=price["price_usd"],
                    start_usage_amount=price["start_usage_amount"],
                    end_usage_amount=price["end_usage_amount"],
                    source="aws-fallback",
                    effective_date=now,
                    fetched_at=now,
                ))
                count += 1

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
