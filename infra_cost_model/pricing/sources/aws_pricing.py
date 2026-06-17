"""AWS Pricing API client for fallback pricing."""

import json
from decimal import Decimal
from datetime import datetime
from typing import Optional

import requests

AWS_PRICE_LIST_URL = "https://pricing.us-east-1.amazonaws.com"


def fetch_aws_price_list(service_code: str) -> list[dict]:
    """Fetch pricing from AWS Price List API.
    
    Args:
        service_code: AWS service code (e.g., 'AWSLambda', 'AmazonDynamoDB')
        
    Returns:
        List of price list items.
    """
    url = f"{AWS_PRICE_LIST_URL}/offers/v1.0/aws/{service_code}/current/index.json"
    
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        results = []
        products = data.get("products", {})
        terms = data.get("terms", {})
        offers = data.get("offers", {})
        
        for sku, product in products.items():
            product_attrs = product.get("attributes", {})
            
            on_demand_terms = terms.get("OnDemand", {}).get(sku, {})
            for term_id, term in on_demand_terms.items():
                for dimension, pricing in term.get("priceDimensions", {}).items():
                    results.append({
                        "sku": sku,
                        "service": service_code,
                        "attributes": product_attrs,
                        "usage_metric": pricing.get("unit", ""),
                        "unit": pricing.get("unit"),
                        "price_usd": float(pricing.get("pricePerUnit", {}).get("USD", 0)),
                    })
        
        return results
    except Exception:
        return []


def aws_fallback_prices(services: list[str], cache, region: str = "us-east-1") -> int:
    """Seed pricing cache from AWS Price List API.
    
    Args:
        services: List of AWS service codes to fetch
        cache: PricingCache instance
        region: AWS region
        
    Returns:
        Number of prices synced.
    """
    from infra_cost_model.pricing.cache import Price
    
    count = 0
    now = datetime.now().isoformat()
    
    service_mapping = {
        "AWSLambda": "AWSLambda",
        "AmazonDynamoDB": "AmazonDynamoDB", 
        "AmazonAPIGatewayHTTP": "AmazonAPIGatewayV2",
        "AmazonBedrock": "AmazonBedrock",
    }
    
    for service in services:
        aws_code = service_mapping.get(service, service)
        price_items = fetch_aws_price_list(aws_code)
        
        for item in price_items:
            price = Price(
                vendor="aws",
                service=service,
                region=region,
                product_family=item.get("attributes", {}).get("productFamily", ""),
                attributes=item.get("attributes", {}),
                usage_metric=item.get("usage_metric", ""),
                unit=item.get("unit", ""),
                price_usd=item.get("price_usd", 0),
                source="aws-pricelist",
                effective_date=now,
                fetched_at=now,
            )
            cache.upsert(price)
            count += 1
    
    return count