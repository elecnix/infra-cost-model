"""Pricing catalog query interface."""

from pathlib import Path
from typing import Optional, Union

from infra_cost_model.pricing.cache import PricingCache, TieredPrice, Price


class PricingCatalog:
    """High-level interface for querying cloud pricing."""
    
    def __init__(self, db_path: str | Path = None):
        self._cache = PricingCache(db_path)
    
    def query(self, vendor: str, service: str, region: str,
              usage_metric: str, usage_quantity: float | None = None) -> Optional[Union["_CostResult", TieredPrice, Price]]:
        """Query pricing for a specific metric.
        
        Args:
            vendor: Cloud provider (aws, azure, gcp)
            service: Service name (e.g., AWSLambda, AmazonDynamoDB)
            region: Region (e.g., us-east-1)
            usage_metric: Metric name (e.g., Lambda-GB-Second, APIGateway-HTTP-Request)
            usage_quantity: Optional quantity for cost calculation
            
        Returns:
            _CostResult if quantity provided, TieredPrice if multiple tiers, Price if single, None if not found
        """
        result = self._cache.query(vendor, service, region, usage_metric)
        
        if result is None:
            return None
        
        # Wrap in _CostResult if quantity provided
        if usage_quantity is not None:
            return _CostResult(result, usage_quantity)
        
        return result


class _CostResult:
    """Result with tiered cost calculation."""
    
    def __init__(self, price_data: Union[TieredPrice, Price], quantity: float):
        self.tiers = price_data.tiers if isinstance(price_data, TieredPrice) else [price_data]
        self.quantity = quantity
        self.total_cost = self._calculate_cost()
    
    def _calculate_cost(self) -> float:
        """Calculate total cost with tiered pricing."""
        total = 0.0
        quantity = self.quantity
        
        for tier in sorted(self.tiers, key=lambda t: t.start_usage_amount or 0):
            tier_start = tier.start_usage_amount or 0
            tier_end = tier.end_usage_amount
            
            # Determine if this tier applies
            if quantity <= tier_start:
                continue
                
            if tier_end is None:
                # Last tier: charge for all quantity above start
                total += max(0, quantity - tier_start) * tier.price_usd
            else:
                # Tier with upper bound
                charged = min(quantity, tier_end) - tier_start
                total += max(0, charged) * tier.price_usd
        
        return max(0, total)