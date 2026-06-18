"""Resource types package.

Public API surface is limited to resource classes and computation utilities.
Standalone cost functions (lambda_cost, dynamodb_cost, apigw_total_cost,
bedrock_cost, stripe_cost, etc.) are private per DP#1 — usage must be derived
through the DAG, not specified as free variables.

Multi-cloud support (DP#6): AWS, GCP, and Azure resource handlers are
registered through the ResourceRegistry with provider-based dispatch.
"""

from .types import ResourceType, ComputeResource, StorageResource, RoutingResource, ExternalResource
from .lambda_func import LambdaFunction, calculate_gb_seconds, apply_free_tier
from .external import ExternalNode, ExternalServiceRegistry
from .gcp import CloudFunction, CloudStorage, CloudRun, Firestore
from .azure import AzureFunction, CosmosDB, APIManagement, AzureOpenAI, AzureBlobStorage

__all__ = [
    "ResourceType",
    "ComputeResource",
    "StorageResource",
    "RoutingResource",
    "ExternalResource",
    "LambdaFunction",
    "calculate_gb_seconds",
    "apply_free_tier",
    "ExternalNode",
    "ExternalServiceRegistry",
    "CloudFunction",
    "CloudStorage",
    "CloudRun",
    "Firestore",
    "AzureFunction",
    "CosmosDB",
    "APIManagement",
    "AzureOpenAI",
    "AzureBlobStorage",
]