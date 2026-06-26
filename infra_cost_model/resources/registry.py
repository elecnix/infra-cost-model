"""
Resource type registry for auto-discovery and code generation.

Implements Principle 10: Type-safe SDK from infrastructure-as-code type generation.
Provides multi-cloud provider dispatch (DP#6).
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
from .sqs import SQSQueue
from .sns import SNSTopic
from .eventbridge import EventBridgeRule
from .cloudfront import CloudFrontDistribution
from .networking import NATGateway, VpcEndpoint
from .rds import RDSInstance
from .cloudwatch import CloudWatchLogGroup
from .ecs import ECSFargateService
from .alb import ApplicationLoadBalancer
from .gcp import CloudFunction, CloudStorage, CloudRun, Firestore
from .azure import AzureFunction, CosmosDB, APIManagement, AzureOpenAI, AzureBlobStorage
from .misc_services import SecretsManagerSecret, ECRRepository, Route53Zone


class ResourceRegistry:
    """Registry for resource type handlers with multi-cloud provider dispatch.

    Auto-registers known resource types and provides:
    - Mapping from resource addresses to handlers
    - Provider-based dispatch (aws, gcp, azure)
    - Validation of usage metrics per resource
    - Node type classification

    Per DP#6, handlers can be registered for any cloud provider. The registry
    supports provider-qualified lookups and can list handlers by provider.
    """

    _handlers: list[Type[ResourceType]] = []
    _provider_index: dict[str, list[Type[ResourceType]]] = {}

    @classmethod
    def register(cls, resource_type: Type[ResourceType]) -> Type[ResourceType]:
        """Register a resource type handler.

        Automatically indexes the handler by provider for fast provider-qualified
        lookups.
        """
        cls._handlers.append(resource_type)
        # Index by provider: derive from class module path
        provider = cls._infer_provider(resource_type)
        if provider:
            cls._provider_index.setdefault(provider, []).append(resource_type)
        return resource_type

    @classmethod
    def _infer_provider(cls, resource_type: Type[ResourceType]) -> Optional[str]:
        """Infer provider from the handler class module path."""
        module = resource_type.__module__
        # Module paths: infra_cost_model.resources.<module>
        parts = module.split(".")
        if len(parts) >= 3 and parts[-2] == "resources":
            leaf = parts[-1]
            known_providers = {
                "lambda_func": "aws", "dynamodb": "aws", "apigw": "aws",
                "bedrock": "aws", "external": "external",
                "s3": "aws", "sqs": "aws", "sns": "aws",
                "eventbridge": "aws", "cloudfront": "aws",
                "rds": "aws", "ecs": "aws",
                "alb": "aws",
                "networking": "aws",
                "rds": "aws",
                "rds": "aws", "misc_services": "aws",
                "gcp": "gcp", "azure": "azure",
            }
            return known_providers.get(leaf)
        return None

    @classmethod
    def from_address(cls, resource_address: str,
                     provider: Optional[str] = None) -> Optional[Type[ResourceType]]:
        """Find the appropriate handler class for a resource address.

        Args:
            resource_address: Resource address from IaC export
            provider: Optional provider hint ("aws", "gcp", "azure") to narrow
                      the search scope. When provided, provider-specific handlers
                      are tried first.

        Returns:
            Matching handler class or None.
        """
        handlers = cls._handlers
        if provider and provider in cls._provider_index:
            # Try provider-specific handlers first, then all handlers as fallback
            handlers = cls._provider_index[provider] + [
                h for h in cls._handlers
                if h not in cls._provider_index.get(provider, [])
            ]

        for handler in handlers:
            result = handler.from_address(resource_address)
            if result is not None:
                return handler
        return None

    @classmethod
    def known_prefixes(cls) -> set[str]:
        """Return set of handler class names registered."""
        return {handler.__name__ for handler in cls._handlers}

    @classmethod
    def handlers_by_provider(cls, provider: str) -> list[Type[ResourceType]]:
        """Return handlers registered for a specific cloud provider.

        Args:
            provider: Cloud provider identifier ("aws", "gcp", "azure")

        Returns:
            List of handler classes for the provider (empty list if none).
        """
        return list(cls._provider_index.get(provider, []))

    @classmethod
    def supported_providers(cls) -> set[str]:
        """Return the set of cloud providers with registered handlers."""
        return set(cls._provider_index.keys())

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
# AWS handlers
ResourceRegistry.register(APIGatewayHTTP)  # More specific patterns first
ResourceRegistry.register(LambdaFunction)
ResourceRegistry.register(DynamoDBTable)
ResourceRegistry.register(S3Bucket)
ResourceRegistry.register(SQSQueue)
ResourceRegistry.register(SNSTopic)
ResourceRegistry.register(EventBridgeRule)
ResourceRegistry.register(CloudFrontDistribution)
ResourceRegistry.register(NATGateway)
ResourceRegistry.register(VpcEndpoint)
ResourceRegistry.register(RDSInstance)
ResourceRegistry.register(CloudWatchLogGroup)
ResourceRegistry.register(ECSFargateService)
ResourceRegistry.register(ApplicationLoadBalancer)
ResourceRegistry.register(BedrockModel)
ResourceRegistry.register(ExternalNode)

# GCP handlers (DP#6: multi-cloud support)
ResourceRegistry.register(CloudRun)
ResourceRegistry.register(CloudFunction)
ResourceRegistry.register(CloudStorage)
ResourceRegistry.register(Firestore)

# Azure handlers (DP#6: multi-cloud support)
ResourceRegistry.register(APIManagement)
ResourceRegistry.register(AzureFunction)
ResourceRegistry.register(CosmosDB)
ResourceRegistry.register(AzureOpenAI)
ResourceRegistry.register(AzureBlobStorage)

# AWS miscellaneous services (Secrets Manager, ECR, Route53)
ResourceRegistry.register(SecretsManagerSecret)
ResourceRegistry.register(ECRRepository)
ResourceRegistry.register(Route53Zone)


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