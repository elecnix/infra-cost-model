"""Resource types for the cost model."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from infra_cost_model.schema.cost_model_schema import validate_cost_model


@dataclass
class ResourceExtract:
    """Extracted resource configuration."""
    resource_address: str
    node_type: str
    provider: str
    service: str
    region: Optional[str]
    config: dict


@dataclass(frozen=True)
class DerivedCatalogUsage:
    """Catalog quantities a handler derives from several usage metrics.

    ``consumed`` names the node's logical usageMetrics keys that feed the
    derivation. ``quantities`` maps catalog usage_metric names to a quantity
    per invocation of the node. It carries usage only, never prices
    (Principle 6).
    """
    consumed: frozenset[str]
    quantities: dict[str, float]


@dataclass
class UsageParams:
    """Usage parameters for a specific resource."""
    resource_address: str
    usage_metrics: dict


class ResourceType(ABC):
    """Base class for resource type definitions."""

    @property
    @abstractmethod
    def node_type(self) -> str:
        """Return the node type (compute, storage, routing)."""
        pass

    @property
    @abstractmethod
    def valid_metrics(self) -> list[str]:
        """Return list of valid usage metric names for this type."""
        pass

    @property
    def catalog_metrics(self) -> dict[str, str]:
        """Map logical usageMetrics names to pricing-catalog usage_metric names.

        Empty by default. Handlers whose logical metrics correspond to catalog
        pricing rows override this so the engine can price nodes from the catalog
        (Principle 13) — using the live/seed pricing — instead of falling back to
        embedded per-node ``pricingRates``. Keyed per handler (not per service),
        because different resources of the same service can reuse a logical name
        for a different catalog metric (e.g. ``dataProcessedGb`` on NAT Gateway
        vs VPC Endpoint).
        """
        return {}

    def catalog_metrics_for(self, config: dict) -> dict[str, str]:
        """``catalog_metrics`` for a node with the resource settings ``config``.

        ``config`` is the node's ``config``, as the ``extract_*`` methods give
        it, or ``{}``. By default it is ``catalog_metrics``. A handler
        overrides this when a setting selects another product, such as the
        model of an Azure OpenAI deployment (#371).
        """
        return self.catalog_metrics

    @property
    def catalog_services(self) -> dict[str, str]:
        """Map catalog usage_metric names to the service whose rows price them.

        Empty by default: the engine prices every metric under the node's
        own service. A handler overrides this when the provider bills a
        quantity under another service. S3 egress to the internet, for
        example, is billed as ``AWSDataTransfer`` data transfer out, so it
        shares the account's free allowance and rate tiers with every other
        node that sends data out (#332).
        """
        return {}

    def derive_catalog_usage(self, usage: dict[str, float],
                             config: Optional[dict] = None) -> Optional[DerivedCatalogUsage]:
        """Derive catalog quantities that depend on more than one usage metric.

        ``usage`` maps the node's logical usageMetrics keys to their values per
        invocation. ``config`` is the node's ``config``, the resource settings
        that can change what the provider bills (#374). ``None`` by default,
        and when an input is missing. A handler
        overrides this when the provider bills a quantity that combines
        several metrics, such as Lambda GB-seconds from duration and memory.
        """
        return None

    @classmethod
    @abstractmethod
    def from_address(cls, resource_address: str) -> Optional["ResourceType"]:
        """Create resource type from Terraform/Pulumi/CDK address."""
        pass

    @classmethod
    @abstractmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        """Extract from Terraform configuration."""
        pass

    @classmethod
    @abstractmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        """Extract from Pulumi stack export."""
        pass

    @classmethod
    @abstractmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        """Extract from CDK CloudFormation template."""
        pass


class ComputeResource(ResourceType):
    """Compute node type - can invoke other nodes (has outgoing edges)."""

    @property
    def node_type(self) -> str:
        return "compute"


class StorageResource(ResourceType):
    """Storage node type - leaf node, cannot invoke other nodes."""

    @property
    def node_type(self) -> str:
        return "storage"


class RoutingResource(ResourceType):
    """Routing node type - can invoke compute/storage nodes."""

    @property
    def node_type(self) -> str:
        return "routing"


class ExternalResource(ResourceType):
    """External node type - leaf node for third-party services.

    Third-party services like Stripe, Twilio, SendGrid have no infrastructure
    to extract. They are leaf nodes with percentage-based or fixed pricing.
    """

    @property
    def node_type(self) -> str:
        return "external"

    @property
    def valid_metrics(self) -> list[str]:
        return ["apiCalls", "transactionVolume", "tokensInput", "tokensOutput"]