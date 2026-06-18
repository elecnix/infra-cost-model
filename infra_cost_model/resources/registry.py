"""
Resource type registry for auto-discovery and code generation.

Implements Principle 10: Type-safe SDK from infrastructure-as-code type generation.
"""

import warnings
from typing import Optional, Type, Dict as DictType

from .types import ResourceType
from .lambda_func import LambdaFunction
from .dynamodb import DynamoDBTable
from .apigw import APIGatewayHTTP
from .bedrock import BedrockModel
from .external import ExternalNode
from .s3 import S3Bucket


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
    def known_prefixes(cls) -> set[str]:
        """Return set of resource address prefixes supported by registered handlers."""
        prefixes = set()
        for handler in cls._handlers:
            # Each handler class has a pattern or set of patterns it matches.
            # Collect the known prefixes by instantiating from known patterns.
            # We expose handler names as a hint for unsupported-resource reporting.
            prefixes.add(handler.__name__)
        return prefixes

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
ResourceRegistry.register(S3Bucket)
ResourceRegistry.register(BedrockModel)
ResourceRegistry.register(ExternalNode)


def extract_resources_from_tf(tf_json: dict) -> dict[str, dict]:
    """Extract all resources from Terraform show -json output.
    
    Args:
        tf_json: Terraform JSON output with 'resource' section
        
    Returns:
        Dict mapping resource addresses to extracted configs.
        
    Emits UserWarning if any resources could not be extracted because
    no handler was registered for their resource type.
    """
    results = {}
    unsupported: list[str] = []
    # Terraform show -json structure
    resources = tf_json.get("resource", []) or tf_json.get("values", {}).get("root_module", {}).get("resources", [])
    
    for resource in resources:
        if isinstance(resource, dict):
            addr = resource.get("address", "")
            if addr:
                extracted = ResourceRegistry.extract(addr, resource, "terraform")
                if extracted:
                    results[addr] = extracted
                else:
                    unsupported.append(addr)
    
    if unsupported:
        warnings.warn(
            f"{len(unsupported)} resource(s) could not be extracted because no handler "
            f"is registered for their resource type. Unsupported addresses: "
            f"{', '.join(sorted(unsupported))}. "
            f"Supported handlers: {sorted(h.__name__ for h in ResourceRegistry._handlers)}. "
            f"To add support, register a new ResourceType handler for the unsupported resource(s)."
        )
    
    return results


def extract_resources_from_pulumi(pulumi_json: dict) -> dict[str, dict]:
    """Extract all resources from Pulumi stack export --json output.
    
    Args:
        pulumi_json: Pulumi stack export JSON
        
    Returns:
        Dict mapping resource addresses to extracted configs.
        
    Emits UserWarning if any resources could not be extracted because
    no handler was registered for their resource type.
    """
    results = {}
    unsupported: list[str] = []
    resources = pulumi_json.get("deployment", {}).get("resources", [])
    
    for resource in resources:
        if isinstance(resource, dict):
            addr = resource.get("id", "") or resource.get("name", "")
            if addr:
                extracted = ResourceRegistry.extract(addr, resource, "pulumi")
                if extracted:
                    results[addr] = extracted
                else:
                    unsupported.append(addr)
    
    if unsupported:
        warnings.warn(
            f"{len(unsupported)} resource(s) could not be extracted because no handler "
            f"is registered for their resource type. Unsupported addresses: "
            f"{', '.join(sorted(unsupported))}. "
            f"Supported handlers: {sorted(h.__name__ for h in ResourceRegistry._handlers)}."
        )
    
    return results


def extract_resources_from_cdk(cdk_json: dict) -> dict[str, dict]:
    """Extract all resources from CDK synth --json output.
    
    CDK synthesizes to CloudFormation templates. The JSON output
    contains a 'Resources' key with CloudFormation logical IDs.
    
    Args:
        cdk_json: CDK synth JSON output (CloudFormation template)
        
    Returns:
        Dict mapping resource addresses to extracted configs.
        
    Emits UserWarning if any resources could not be extracted because
    no handler was registered for their resource type.
    """
    results = {}
    unsupported: list[str] = []
    resources = cdk_json.get("Resources", {})
    
    for logical_id, resource in resources.items():
        if isinstance(resource, dict):
            # CDK uses CloudFormation format: logical ID + Type + Properties
            resource_type = resource.get("Type", "")
            # Build a synthetic address from the CloudFormation type and logical ID
            addr = f"{resource_type}:{logical_id}"
            extracted = ResourceRegistry.extract(addr, resource, "cdk")
            if extracted:
                results[addr] = extracted
            else:
                unsupported.append(addr)
    
    if unsupported:
        warnings.warn(
            f"{len(unsupported)} resource(s) could not be extracted because no handler "
            f"is registered for their resource type. Unsupported addresses: "
            f"{', '.join(sorted(unsupported))}. "
            f"Supported handlers: {sorted(h.__name__ for h in ResourceRegistry._handlers)}."
        )
    
    return results


def known_node_types() -> list[str]:
    """Return list of known node types."""
    return ["compute", "storage", "routing", "external"]


def is_leaf_node(node_type: str) -> bool:
    """Check if a node type is a leaf (cannot have outgoing edges)."""
    return node_type in ("storage", "external")