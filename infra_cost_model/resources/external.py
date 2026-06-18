"""External API resource model for third-party services (Stripe, Twilio, SendGrid)."""

from dataclasses import dataclass
from typing import Optional

from .types import ExternalResource, ResourceExtract


@dataclass
class ExternalPricing:
    """External service pricing configuration."""
    percentage_rate: float = 0.0  # e.g., 0.029 for 2.9%
    fixed_per_transaction: float = 0.0  # e.g., 0.30 for $0.30 per transaction
    per_call: float = 0.0  # Fixed per-call pricing (e.g., Twilio)


class ExternalNode(ExternalResource):
    """Third-party service node - economic sink with no infrastructure.
    
    External nodes cannot be extracted from .tf/Pulumi/CDK since they
    represent services outside the user's infrastructure.
    """
    
    @classmethod
    def from_address(cls, resource_address: str) -> Optional["ExternalNode"]:
        """Parse resource address to determine if it's an external service."""
        # External services are typically referenced by logical names
        if resource_address.startswith("external.") or \
           resource_address.startswith("stripe.") or \
           resource_address.startswith("twilio.") or \
           resource_address.startswith("sendgrid."):
            return cls()
        return None
    
    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        """External services cannot be extracted from Terraform - they have no resource."""
        raise NotImplementedError(
            "External services have no infrastructure resource to extract. "
            "Define them directly in the cost model YAML or SDK."
        )
    
    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        """External services cannot be extracted from Pulumi - they have no resource."""
        raise NotImplementedError(
            "External services have no infrastructure resource to extract. "
            "Define them directly in the cost model YAML or SDK."
        )
    
    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        """External services cannot be extracted from CDK - they have no resource."""
        raise NotImplementedError(
            "External services have no infrastructure resource to extract. "
            "Define them directly in the cost model YAML or SDK."
        )


def external_cost(transactions: float, volume: float,
                  percentage_rate: float = 0.0,
                  fixed_per_transaction: float = 0.0,
                  per_call: float = 0.0,
                  catalog=None) -> float:
    """Calculate external service cost.
    
    Args:
        transactions: Number of transactions per month
        volume: Transaction volume in USD
        percentage_rate: Percentage taken of volume (e.g., 0.029 for 2.9%)
        fixed_per_transaction: Fixed fee per transaction (e.g., 0.30)
        per_call: Fixed price per API call
        catalog: Optional pricing catalog for lookup
        
    Returns:
        Total monthly cost in USD.
    """
    if catalog:
        # Use catalog if available
        result = catalog.query(
            vendor="external",
            service="ExternalAPI",
            region="global",
            usage_metric="external_transaction",
            usage_quantity=transactions,
        )
        if result and hasattr(result, 'total_cost'):
            return result.total_cost
    
    # Percentage + fixed per transaction (Stripe model)
    percentage_cost = volume * percentage_rate
    fixed_cost = transactions * fixed_per_transaction
    
    # Per-call pricing (Twilio/SendGrid model)
    call_cost = transactions * per_call
    
    return percentage_cost + fixed_cost + call_cost


# Canonical external pricing constants (fallback when no catalog is available).
# These match the seed pricing file entries in data/seed/aws_pricelist_seed.json.
# Prefer catalog.query() over these constants per Principle 13.
_STRIPE_STANDARD_PCT = 0.029
_STRIPE_STANDARD_FIXED = 0.30
_STRIPE_INTERNATIONAL_PCT = 0.039
_STRIPE_INTERNATIONAL_FIXED = 0.30
_TWILIO_SMS_RATE = 0.0075
_SENDGRID_EMAIL_RATE = 0.0001


def stripe_cost(transactions: float, volume: float, international: bool = False,
                catalog=None) -> float:
    """Calculate Stripe cost.
    
    Prices come from the pricing catalog when available (Principle 13).
    Falls back to canonical Stripe pricing constants when no catalog is provided.
    
    Args:
        transactions: Number of charges
        volume: Transaction volume in USD
        international: Whether cards are international (+1% fee)
        catalog: Optional PricingCatalog for price lookup
        
    Returns:
        Total Stripe fee in USD.
    """
    if catalog is not None:
        pct_metric = "stripe_international_percentage" if international else "external_percentage"
        fixed_metric = "stripe_international_fixed_per_tx" if international else "external_fixed_per_tx"

        pct_result = catalog.query("external", "ExternalAPI", "global", pct_metric, volume)
        fixed_result = catalog.query("external", "ExternalAPI", "global", fixed_metric, transactions)

        pct_cost = pct_result.total_cost if pct_result else volume * (0.039 if international else 0.029)
        fixed_cost = fixed_result.total_cost if fixed_result else transactions * 0.30
        base_cost = pct_cost + fixed_cost
    else:
        # Canonical fallback: Stripe standard 2.9% + $0.30, international 3.9% + $0.30
        rate = 0.039 if international else 0.029
        base_cost = volume * rate + transactions * 0.30

    if international:
        base_cost += volume * 0.01  # Additional currency conversion fee

    return base_cost


def twilio_sms_cost(messages: float, catalog=None) -> float:
    """Calculate Twilio SMS cost.
    
    Prices come from the pricing catalog when available (Principle 13).
    Falls back to canonical Twilio pricing when no catalog is provided.
    
    Args:
        messages: Number of SMS messages sent
        catalog: Optional PricingCatalog for price lookup
        
    Returns:
        Total cost in USD.
    """
    if catalog is not None:
        result = catalog.query("external", "ExternalAPI", "global", "twilio_sms", messages)
        if result is not None:
            return result.total_cost

    # Canonical fallback: $0.0075 per SMS
    return messages * 0.0075


def sendgrid_cost(emails: float, catalog=None) -> float:
    """Calculate SendGrid cost.
    
    Prices come from the pricing catalog when available (Principle 13).
    Falls back to canonical SendGrid pricing when no catalog is provided.
    
    Args:
        emails: Number of emails sent
        catalog: Optional PricingCatalog for price lookup
        
    Returns:
        Total cost in USD.
    """
    if catalog is not None:
        result = catalog.query("external", "ExternalAPI", "global", "sendgrid_email", emails)
        if result is not None:
            return result.total_cost

    # Canonical fallback: $0.0001 per email
    return emails * 0.0001