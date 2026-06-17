"""Infracost Cloud Pricing API client."""

import os
import json
import requests
from pathlib import Path
from datetime import datetime
from typing import Optional

INFCOST_API_URL = "https://pricing-api.infracost.io"
INFCOST_CONFIG_DIR = Path.home() / ".config" / "infracost"
INFCOST_TOKEN_FILE = INFCOST_CONFIG_DIR / "token.json"


class InfracostClient:
    """GraphQL client for Infracost Cloud Pricing API."""
    
    def __init__(self, api_url: str = None):
        self.api_url = api_url or INFCOST_API_URL
        self._token: Optional[str] = None
        self._org_id: Optional[str] = None
    
    def _load_token(self) -> bool:
        if self._token:
            return True
        if not INFCOST_TOKEN_FILE.exists():
            return False
        try:
            data = json.loads(INFCOST_TOKEN_FILE.read_text())
            self._token = data.get("accessToken")
            self._org_id = data.get("orgId")
            return bool(self._token)
        except (json.JSONDecodeError, KeyError):
            return False
    
    def _ensure_auth(self) -> bool:
        if self._load_token():
            return True
        if os.getenv("INFRACOST_API_KEY"):
            self._token = os.getenv("INFRACOST_API_KEY")
            return True
        return False
    
    def query_prices(self, vendor: str, service: str, region: str, 
                     usage_metric: str) -> list[dict]:
        if not self._ensure_auth():
            raise RuntimeError(
                "Infracost auth not found. Run 'infracost auth login' or set INFRACOST_API_KEY"
            )
        
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        
        query = """
        query($vendor: String!, $service: String!, $region: String!) {
            products(
                filter: {vendor: $vendor, service: $service, region: $region}
            ) {
                product_name
                product_family
                attributes
                prices {
                    USD
                    usage_metric {
                        name
                        unit
                    }
                    start_usage_amount
                    end_usage_amount
                }
            }
        }
        """
        
        variables = {"vendor": vendor, "service": service, "region": region}
        
        response = requests.post(
            self.api_url,
            headers=headers,
            json={"query": query, "variables": variables}
        )
        
        if response.status_code == 401:
            raise RuntimeError("Authentication failed. Run 'infracost auth login' for fresh token.")
        
        response.raise_for_status()
        data = response.json()
        
        products = data.get("data", {}).get("products", [])
        
        results = []
        for product in products:
            for price in product.get("prices", []):
                metric = price.get("usage_metric", {})
                if metric.get("name") == usage_metric:
                    results.append({
                        "vendor": vendor,
                        "service": service,
                        "region": region,
                        "product_family": product.get("product_family"),
                        "attributes": product.get("attributes", {}),
                        "usage_metric": usage_metric,
                        "unit": metric.get("unit"),
                        "price_usd": price.get("USD"),
                        "start_usage_amount": price.get("start_usage_amount"),
                        "end_usage_amount": price.get("end_usage_amount"),
                        "source": "infracost",
                    })
        
        return results
    
    def sync_to_cache(self, cache, vendor: str, service: str, region: str,
                      usage_metric: str) -> int:
        from infra_cost_model.pricing.cache import Price
        prices = self.query_prices(vendor, service, region, usage_metric)
        
        count = 0
        now = datetime.now().isoformat()
        for p in prices:
            price = Price(
                vendor=p["vendor"],
                service=p["service"],
                region=p["region"],
                product_family=p["product_family"],
                attributes=p["attributes"],
                usage_metric=p["usage_metric"],
                unit=p["unit"],
                price_usd=p["price_usd"],
                start_usage_amount=p["start_usage_amount"],
                end_usage_amount=p["end_usage_amount"],
                source=p["source"],
                effective_date=now,
                fetched_at=now,
            )
            cache.upsert(price)
            count += 1
        
        return count


def sync_pricing_catalog(vendor: str = "aws", services: list[str] = None,
                         fallback: bool = False) -> tuple[int, str]:
    from infra_cost_model.pricing.cache import PricingCache
    
    cache = PricingCache()
    
    if fallback:
        return _sync_fallback(vendor, services, cache)
    
    client = InfracostClient()
    try:
        client._ensure_auth()
        if not client._token:
            return _sync_fallback(vendor, services, cache)
    except Exception:
        return _sync_fallback(vendor, services, cache)
    
    if services is None:
        services = ["AWSLambda", "AmazonDynamoDB", "AmazonAPIGatewayHTTP", "AmazonBedrock"]
    
    region = "us-east-1"
    total = 0
    
    service_metrics = {
        "AWSLambda": ["Lambda-Request", "Lambda-GB-Second"],
        "AmazonDynamoDB": ["Dynamo-ReadRequest", "Dynamo-WriteRequest", "Dynamo-Storage"],
        "AmazonAPIGatewayHTTP": ["APIGateway-HTTP-Request"],
        "AmazonBedrock": ["Bedrock-Input-Token", "Bedrock-Output-Token"],
    }
    
    for service in services:
        for metric in service_metrics.get(service, []):
            try:
                total += client.sync_to_cache(cache, vendor, service, region, metric)
            except RuntimeError:
                return _sync_fallback(vendor, services, cache)
    
    return total, "infracost"


def _sync_fallback(vendor: str, services: list[str], cache) -> tuple[int, str]:
    from infra_cost_model.pricing.cache import Price
    now = datetime.now().isoformat()
    
    fallback_prices = {
        "AWSLambda": [
            Price(
                vendor="aws", service="AWSLambda", region="us-east-1",
                product_family="Serverless", attributes={},
                usage_metric="Lambda-Request", unit="requests",
                price_usd=0.0,
                start_usage_amount=0, end_usage_amount=1_000_000,
                source="aws-fallback", effective_date=now, fetched_at=now
            ),
            Price(
                vendor="aws", service="AWSLambda", region="us-east-1",
                product_family="Serverless", attributes={},
                usage_metric="Lambda-Request", unit="requests",
                price_usd=0.20e-6,
                start_usage_amount=1_000_000, end_usage_amount=None,
                source="aws-fallback", effective_date=now, fetched_at=now
            ),
            Price(
                vendor="aws", service="AWSLambda", region="us-east-1",
                product_family="Serverless", attributes={},
                usage_metric="Lambda-GB-Second", unit="GB-s",
                price_usd=0.0000166667,
                start_usage_amount=0, end_usage_amount=6_000_000_000,
                source="aws-fallback", effective_date=now, fetched_at=now
            ),
        ],
        "AmazonDynamoDB": [
            Price(
                vendor="aws", service="AmazonDynamoDB", region="us-east-1",
                product_family="OnDemand", attributes={},
                usage_metric="Dynamo-ReadRequest", unit="requests",
                price_usd=1.25e-6,
                start_usage_amount=0, end_usage_amount=None,
                source="aws-fallback", effective_date=now, fetched_at=now
            ),
            Price(
                vendor="aws", service="AmazonDynamoDB", region="us-east-1",
                product_family="OnDemand", attributes={},
                usage_metric="Dynamo-WriteRequest", unit="requests",
                price_usd=6.25e-6,
                start_usage_amount=0, end_usage_amount=None,
                source="aws-fallback", effective_date=now, fetched_at=now
            ),
            Price(
                vendor="aws", service="AmazonDynamoDB", region="us-east-1",
                product_family="Storage", attributes={},
                usage_metric="Dynamo-Storage", unit="GB-Mo",
                price_usd=0.25,
                start_usage_amount=0, end_usage_amount=None,
                source="aws-fallback", effective_date=now, fetched_at=now
            ),
        ],
        "AmazonAPIGatewayHTTP": [
            Price(
                vendor="aws", service="AmazonAPIGatewayHTTP", region="us-east-1",
                product_family="APIGateway", attributes={},
                usage_metric="APIGateway-HTTP-Request", unit="requests",
                price_usd=1.00e-6,
                start_usage_amount=0, end_usage_amount=None,
                source="aws-fallback", effective_date=now, fetched_at=now
            ),
        ],
        "AmazonBedrock": [
            Price(
                vendor="aws", service="AmazonBedrock", region="us-east-1",
                product_family="LLM", attributes={},
                usage_metric="Bedrock-Input-Token", unit="tokens",
                price_usd=0.003 / 1000,
                start_usage_amount=0, end_usage_amount=None,
                source="aws-fallback", effective_date=now, fetched_at=now
            ),
            Price(
                vendor="aws", service="AmazonBedrock", region="us-east-1",
                product_family="LLM", attributes={},
                usage_metric="Bedrock-Output-Token", unit="tokens",
                price_usd=0.015 / 1000,
                start_usage_amount=0, end_usage_amount=None,
                source="aws-fallback", effective_date=now, fetched_at=now
            ),
        ],
    }
    
    count = 0
    services_to_sync = services or list(fallback_prices.keys())
    
    for service in services_to_sync:
        for price in fallback_prices.get(service, []):
            cache.upsert(price)
            count += 1
    
    return count, "fallback"