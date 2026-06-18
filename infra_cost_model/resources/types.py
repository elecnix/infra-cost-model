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
    to extract. They are economic sinks with percentage-based or fixed pricing.
    """
    
    @property
    def node_type(self) -> str:
        return "external"
    
    @property
    def valid_metrics(self) -> list[str]:
        return ["apiCalls", "transactionVolume", "tokensInput", "tokensOutput"]