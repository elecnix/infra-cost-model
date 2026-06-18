"""
Resource type registry for auto-discovery and code generation.

Implements Principle 10: Type-safe SDK from infrastructure-as-code type generation.
"""

from typing import Optional, Type, Dict as DictType

from .types import ResourceType
from .lambda_func import LambdaFunction
from .dynamodb import DynamoDBTable
from .apigw import APIGatewayHTTP
from .bedrock import BedrockModel
from .external import ExternalNode


class ResourceRegistry:
    """Registry for resource type handlers.
    
    Auto-registers known resource types and provides:
    - Mapping from resource addresses to handlers
    - Validation of usage metrics per resource
    - Node type classification
    """
    
    _handlers: list[Type[ResourceType]] = []
    
    @classmethod
    def register(cls, resource_type: Type[ResourceType]) -> Type[ResourceType]:
        """Register a resource type handler."""
        cls._handlers.append(resource_type)
        return resource_type
    
    @classmethod
    def from_address(cls, resource_address: str) -> Optional[Type[ResourceType]]:
        """Find the appropriate handler class for a resource address."""
        for handler in cls._handlers:
            result = handler.from_address(resource_address)
            if result is not None:
                return handler
        return None
    
    @classmethod
    def extract(cls, resource_address: str, resource_data: dict, 
                source_format: str = "terraform") -> Optional[dict]:
        """Extract resource configuration using the appropriate handler.
        
        Args:
            resource_address: Full resource address
            resource_data: Raw resource data from IaC export
            source_format: "terraform", "pulumi", or "cdk"
            
        Returns:
            Extracted resource dict or None if unsupported.
        """
        handler = cls.from_address(resource_address)
        if not handler:
            return None
        
        extract_methods = {
            "terraform": "extract_tf",
            "pulumi": "extract_pulumi",
            "cdk": "extract_cdk",
        }
        
        method = getattr(handler, extract_methods.get(source_format, "extract_tf"), None)
        if method:
            try:
                result = method(resource_data)
                return {
                    "nodeType": result.node_type,
                    "resourceAddress": result.resource_address,
                    "provider": result.provider,
                    "service": result.service,
                    "region": result.region,
                    "config": result.config,
                }
            except NotImplementedError:
                return None
        return None


# Register known resource types in order of specificity
ResourceRegistry.register(APIGatewayHTTP)  # More specific patterns first
ResourceRegistry.register(LambdaFunction)
ResourceRegistry.register(DynamoDBTable)
ResourceRegistry.register(BedrockModel)
ResourceRegistry.register(ExternalNode)


def extract_resources_from_tf(tf_json: dict) -> dict[str, dict]:
    """Extract all resources from Terraform show -json output.
    
    Args:
        tf_json: Terraform JSON output with 'resource' section
        
    Returns:
        Dict mapping resource addresses to extracted configs.
    """
    results = {}
    # Terraform show -json structure
    resources = tf_json.get("resource", []) or tf_json.get("values", {}).get("root_module", {}).get("resources", [])
    
    for resource in resources:
        if isinstance(resource, dict):
            addr = resource.get("address", "")
            if addr:
                extracted = ResourceRegistry.extract(addr, resource, "terraform")
                if extracted:
                    results[addr] = extracted
    
    return results


def extract_resources_from_pulumi(pulumi_json: dict) -> dict[str, dict]:
    """Extract all resources from Pulumi stack export --json output.
    
    Args:
        pulumi_json: Pulumi stack export JSON
        
    Returns:
        Dict mapping resource addresses to extracted configs.
    """
    results = {}
    resources = pulumi_json.get("deployment", {}).get("resources", [])
    
    for resource in resources:
        if isinstance(resource, dict):
            addr = resource.get("id", "") or resource.get("name", "")
            if addr:
                extracted = ResourceRegistry.extract(addr, resource, "pulumi")
                if extracted:
                    results[addr] = extracted
    
    return results


def known_node_types() -> list[str]:
    """Return list of known node types."""
    return ["compute", "storage", "routing", "external"]


def is_leaf_node(node_type: str) -> bool:
    """Check if a node type is a leaf (cannot have outgoing edges)."""
    return node_type in ("storage", "external")