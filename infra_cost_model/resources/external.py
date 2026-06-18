"""External API resource model for third-party services (Stripe, Twilio, SendGrid)."""

from dataclasses import dataclass
from typing import Optional

from .types import ExternalResource, ResourceExtract


class ExternalServiceRegistry:
    """Data-driven registry of known external service prefixes.
    
    Replaces the hardcoded if/elif prefix chain with a registration
    mechanism comparable to ResourceRegistry. New external vendors
    (Auth0, OpenAI, Datadog, etc.) can be added without editing the
    from_address() conditional.
    """
    
    _prefixes: set[str] = set()
    
    @classmethod
    def register(cls, prefix: str) -> None:
        """Register an external service prefix.
        
        Args:
            prefix: Address prefix (e.g., "stripe.", "auth0.").
                    Trailing dot is automatically added if missing.
        """
        if not prefix.endswith("."):
            prefix = prefix + "."
        cls._prefixes.add(prefix)
    
    @classmethod
    def register_many(cls, prefixes: list[str]) -> None:
        """Register multiple external service prefixes at once."""
        for prefix in prefixes:
            cls.register(prefix)
    
    @classmethod
    def is_external(cls, resource_address: str) -> bool:
        """Check if a resource address matches any known external service."""
        for prefix in cls._prefixes:
            if resource_address.startswith(prefix):
                return True
        return False
    
    @classmethod
    def known_prefixes(cls) -> set[str]:
        """Return the set of registered prefixes."""
        return cls._prefixes.copy()
    
    @classmethod
    def reset(cls) -> None:
        """Clear all registered prefixes (primarily for testing)."""
        cls._prefixes.clear()


# Register the built-in external services
ExternalServiceRegistry.register_many(["external", "stripe", "twilio", "sendgrid"])


@dataclass
class ExternalPricing:
    """External service pricing configuration."""
    percentage_rate: float = 0.0  # e.g., 0.029 for 2.9%
    fixed_per_transaction: float = 0.0  # e.g., 0.30 for $0.30 per transaction
    per_call: float = 0.0  # Fixed per-call pricing (e.g., Twilio)


class ExternalNode(ExternalResource):
    """Third-party service node - leaf node with no infrastructure.
    
    External nodes cannot be extracted from .tf/Pulumi/CDK since they
    represent services outside the user's infrastructure.
    """
    
    @classmethod
    def from_address(cls, resource_address: str) -> Optional["ExternalNode"]:
        """Parse resource address to determine if it's an external service."""
        if ExternalServiceRegistry.is_external(resource_address):
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


def _external_cost(transactions: float, volume: float,
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


# Predefined external service configurations
STRIPE_STANDARD = {"percentage_rate": 0.029, "fixed_per_transaction": 0.30}
STRIPE_INTERNATIONAL = {"percentage_rate": 0.039, "fixed_per_transaction": 0.30}


def _stripe_cost(transactions: float, volume: float, international: bool = False) -> float:
    """Calculate Stripe cost.
    
    Args:
        transactions: Number of charges
        volume: Transaction volume in USD
        international: Whether cards are international (+1% fee)
        
    Returns:
        Total Stripe fee in USD.
    """
    config = STRIPE_INTERNATIONAL if international else STRIPE_STANDARD
    
    base_cost = volume * config["percentage_rate"] + transactions * config["fixed_per_transaction"]
    
    if international:
        base_cost += volume * 0.01  # Additional currency conversion fee
    
    return base_cost


TWILIO_SMS_RATES = {"per_call": 0.0075}  # $0.0075 per SMS


def _twilio_sms_cost(messages: float) -> float:
    """Calculate Twilio SMS cost.
    
    Args:
        messages: Number of SMS messages sent
        
    Returns:
        Total cost in USD.
    """
    return messages * TWILIO_SMS_RATES["per_call"]


SENDGRID_RATES = {"per_call": 0.0001}  # $0.0001 per email (approximate)


def _sendgrid_cost(emails: float) -> float:
    """Calculate SendGrid cost.
    
    Args:
        emails: Number of emails sent
        
    Returns:
        Total cost in USD.
    """
    return emails * SENDGRID_RATES["per_call"]