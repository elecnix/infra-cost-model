"""Pricing package for infra-cost-model."""

from .cache import PricingCache, Price, TieredPrice
from .catalog import PricingCatalog, _CostResult
from .github_copilot import (
    CopilotPlan,
    CopilotModelRate,
    COPILOT_PLANS,
    COPILOT_MODEL_RATES,
    CREDIT_VALUE_USD,
    get_copilot_plan,
    list_copilot_plans,
    estimate_credits,
    copilot_plan_cost,
    compute_copilot_cost,
)
from .identity_providers import (
    IdentityProviderPricing,
    MAUTier,
    SSOTier,
    IDENTITY_PROVIDER_PRICING,
    get_identity_provider,
    list_identity_providers,
    compute_mau_cost,
    compute_sso_cost,
    compute_total_cost,
)

__all__ = [
    "PricingCache", "Price", "TieredPrice", "PricingCatalog", "_CostResult",
    "CopilotPlan", "CopilotModelRate",
    "COPILOT_PLANS", "COPILOT_MODEL_RATES", "CREDIT_VALUE_USD",
    "get_copilot_plan", "list_copilot_plans",
    "estimate_credits", "copilot_plan_cost", "compute_copilot_cost",
    "IdentityProviderPricing", "MAUTier", "SSOTier",
    "IDENTITY_PROVIDER_PRICING",
    "get_identity_provider", "list_identity_providers",
    "compute_mau_cost", "compute_sso_cost", "compute_total_cost",
]
