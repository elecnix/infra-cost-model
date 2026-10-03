"""External API resource model for third-party services (Stripe, Twilio, SendGrid)."""

from dataclasses import dataclass
from typing import Optional


from .types import ExternalResource, ResourceExtract


class ExternalServiceRegistry:
    """The set of vendor address prefixes that count as external services.

    A third-party vendor is external when its address starts with one of these
    prefixes. ``ExternalNode.from_address`` is the only caller: the resource
    registry dispatches an address here, and this decides whether a Node with no
    infrastructure belongs to the vendor or to a cloud provider.
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



