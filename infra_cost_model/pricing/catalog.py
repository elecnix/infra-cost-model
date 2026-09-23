"""Pricing catalog query interface."""

from pathlib import Path
from typing import Optional, Union

from infra_cost_model.pricing.cache import PricingCache, TieredPrice, Price


class PricingCatalog:
    """High-level interface for querying cloud pricing."""

    def __init__(self, db_path: str | Path = None, seed: bool = False):
        self._cache = PricingCache(db_path, seed=seed)

    def source_info(self) -> dict[str, int]:
        """Return a count of rows by pricing source (infracost, seed, etc.).

        An empty dict means no rows are cached at all.
        """
        return self._cache.source_info()

    def query(self, vendor: str, service: str, region: str,
              usage_metric: str, usage_quantity: float | None = None,
              parameters: dict[str, float] = None,
              include_free_tier: bool = True) -> Optional[Union["_CostResult", TieredPrice, Price]]:
        """Query pricing for a specific metric.

        Args:
            vendor: Cloud provider (aws, azure, gcp)
            service: Service name (e.g., AWSLambda, AmazonDynamoDB)
            region: Region (e.g., us-east-1)
            usage_metric: Metric name (e.g., Lambda-GB-Second, APIGateway-HTTP-Request)
            usage_quantity: Optional quantity for cost calculation
            parameters: Optional parameters for resolving 'per' multipliers
            include_free_tier: When False, treat the leading $0 tiers as
                already used, so the quantity is priced from the first paid
                tier. Only applies when usage_quantity is given.

        Returns:
            _CostResult if quantity provided, TieredPrice if multiple tiers, Price if single, None if not found
        """
        result = self._cache.query(vendor, service, region, usage_metric)

        if result is None:
            return None

        # Wrap in _CostResult if quantity provided
        if usage_quantity is not None:
            return _CostResult(result, usage_quantity, parameters,
                               include_free_tier=include_free_tier)

        return result


class _CostResult:
    """Result with tiered cost calculation."""

    def __init__(self, price_data: Union[TieredPrice, Price], quantity: float,
                 parameters: dict[str, float] = None, include_free_tier: bool = True):
        self.price_data = price_data
        self.tiers = price_data.tiers if isinstance(price_data, TieredPrice) else [price_data]
        self.quantity = quantity
        self.parameters = parameters or {}
        self.include_free_tier = include_free_tier
        self.total_cost = self._calculate_cost()

    def _multiplier(self, tier) -> float:
        """Resolve a tier's 'per' multiplier, which scales its boundaries."""
        if tier.per and tier.per in self.parameters:
            return self.parameters[tier.per]
        return 1.0

    def _free_tier_end(self) -> float:
        """Where the leading run of $0 tiers ends, or 0 if there is none."""
        end = 0.0
        for tier in sorted(self.tiers, key=lambda t: t.start_usage_amount or 0):
            if tier.price_usd != 0 or tier.end_usage_amount is None:
                break
            if (tier.start_usage_amount or 0) * self._multiplier(tier) > end:
                break
            end = tier.end_usage_amount * self._multiplier(tier)
        return end

    def _calculate_cost(self) -> float:
        """Calculate total cost with tiered pricing."""
        total = 0.0
        quantity = self.quantity
        if not self.include_free_tier:
            # Shift the quantity past the free allowance, so each unit is
            # priced as if the allowance were already used up.
            quantity += self._free_tier_end()

        # Check if all tiers have None start_usage_amount (flat price)
        all_null_start = all(t.start_usage_amount is None for t in self.tiers)

        if all_null_start:
            # Simple flat price - average of all prices * quantity
            avg_price = sum(t.price_usd for t in self.tiers) / len(self.tiers)
            return quantity * avg_price

        for tier in sorted(self.tiers, key=lambda t: t.start_usage_amount or 0):
            # Resolve 'per' multiplier for boundaries only
            multiplier = self._multiplier(tier)

            tier_start = (tier.start_usage_amount or 0) * multiplier
            tier_end = (tier.end_usage_amount * multiplier) if tier.end_usage_amount is not None else None
            price = tier.price_usd

            # Determine if this tier applies
            if quantity <= tier_start:
                continue

            if tier_end is None:
                # Last tier: charge for all quantity above start
                total += max(0, quantity - tier_start) * price
            else:
                # Tier with upper bound
                charged = min(quantity, tier_end) - tier_start
                total += max(0, charged) * price

        return max(0, total)