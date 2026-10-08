"""
Resource type registry for auto-discovery and code generation.

Implements Principle 10: Type-safe SDK from infrastructure-as-code type generation.
Provides multi-cloud provider dispatch (DP#6).
"""

import warnings
from typing import Iterable, Optional, Type, Dict as DictType

from .types import LEGACY_CATALOG_METRICS, DerivedCatalogUsage, ResourceType
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
from .networking import NATGateway, VpcEndpoint, ElasticIP
from .rds import RDSInstance
from .cloudwatch import CloudWatchLogGroup, CloudWatchMetricAlarm
from .ecs import ECSFargateService
from .alb import ApplicationLoadBalancer
from .gcp import CloudFunction, CloudFunctionGen2, CloudStorage, CloudRun, Firestore
from .azure import (
    AppServicePlan, AzureFunction, CosmosDB, APIManagement, AzureOpenAI, AzureOpenAIDeployment,
    AzureBlobStorage, ARM_ADDRESS_KEY, ARM_PARAMETERS_KEY, COGNITIVE_ACCOUNTS_KEY,
    COSMOS_THROUGHPUT_KEY, SERVICE_PLANS_KEY, cognitive_accounts_from_arm,
    cognitive_accounts_from_pulumi, cognitive_accounts_from_tf, cosmos_throughput_from_arm,
    cosmos_throughput_from_pulumi, cosmos_throughput_from_tf, resolve_arm_value,
    service_plans_from_arm, service_plans_from_pulumi, service_plans_from_tf,
)
from .misc_services import SecretsManagerSecret, ECRRepository, Route53Zone
from .kms import KMSKey
from .waf import WAFv2WebACL
from .data_transfer import DataTransferNode


class ResourceRegistry:
    """Registry for resource type handlers with multi-cloud provider dispatch.

    Auto-registers known resource types and provides:
    - Mapping from resource addresses to handlers
    - Validation of usage metrics per resource
    - Node type classification

    Per DP#6, handlers can be registered for any cloud provider. Each handler
    owns the addresses it claims, so lookup asks every registered handler and
    the first match wins; registration order is most-specific first.
    """

    _handlers: list[Type[ResourceType]] = []

    @classmethod
    def register(cls, resource_type: Type[ResourceType]) -> Type[ResourceType]:
        """Register a resource type handler."""
        cls._handlers.append(resource_type)
        return resource_type

    @classmethod
    def from_address(cls, resource_address: str) -> Optional[Type[ResourceType]]:
        """Find the appropriate handler class for a resource address.

        Args:
            resource_address: Resource address from IaC export

        Returns:
            Matching handler class or None.
        """
        for handler in cls._handlers:
            result = handler.from_address(resource_address)
            if result is not None:
                return handler
        return None

    @classmethod
    def valid_metrics_for(cls, resource_address: str) -> Optional[list[str]]:
        """The logical ``usageMetrics`` names the handler for ``resource_address``
        accepts, in the order the handler declares them.

        The registry is the single owner of a Node type's metric vocabulary:
        a model names one of these, never a pricing-catalog row name (#427).
        Returns ``None`` when no handler matches the address.
        """
        handler = cls.from_address(resource_address)
        if handler is None:
            return None
        return list(handler().valid_metrics)

    @classmethod
    def unknown_metrics(cls, resource_address: str,
                        metrics: Iterable[str]) -> list[str]:
        """Names in ``metrics`` that are outside the handler's vocabulary, in the
        order given, each reported once.

        Advisory: the engine still prices a raw catalog row name it finds rows
        for, so a model written before the logical vocabulary keeps working.
        This is what tells such a model to migrate (#427). A node with no
        handler has no vocabulary, so nothing is reported.
        """
        allowed = cls.valid_metrics_for(resource_address)
        if allowed is None:
            return []
        allowed = set(allowed) | LEGACY_CATALOG_METRICS
        seen, unknown = set(), []
        for name in metrics:
            if name not in allowed and name not in seen:
                seen.add(name)
                unknown.append(name)
        return unknown

    @classmethod
    def resolve_catalog_metric(cls, resource_address: str,
                               logical_metric: str,
                               config: Optional[dict] = None) -> Optional[str]:
        """Map a node's logical usageMetrics key to a catalog usage_metric name.

        Finds the handler that owns ``resource_address`` and looks up
        ``logical_metric`` in its ``catalog_metrics_for(config)`` map, where
        ``config`` is the node's ``config`` (#371). Resolution is
        per-handler (not per-service) so resources sharing a service can reuse a
        logical name for different catalog metrics (e.g. ``dataProcessedGb`` maps
        to ``NAT-Gateway-DataProcessed`` for NAT Gateway but
        ``VPC-Endpoint-DataProcessed`` for a VPC endpoint).

        Returns ``None`` when no handler matches or the handler has no mapping for
        that logical name.
        """
        handler = cls.from_address(resource_address)
        if handler is None:
            return None
        return handler().catalog_metrics_for(config or {}).get(logical_metric)

    @classmethod
    def resolve_catalog_service(cls, resource_address: str,
                                catalog_metric: str) -> Optional[str]:
        """The service whose rows price ``catalog_metric`` for the handler
        that owns ``resource_address``, when it isn't the node's service.

        Returns ``None`` when no handler matches or the handler prices the
        metric under the node's own service.
        """
        handler = cls.from_address(resource_address)
        if handler is None:
            return None
        return handler().catalog_services.get(catalog_metric)

    @classmethod
    def derive_catalog_usage(cls, resource_address: str,
                             usage: dict[str, float],
                             config: Optional[dict] = None) -> Optional[DerivedCatalogUsage]:
        """Ask the handler that owns ``resource_address`` for derived catalog
        quantities. Returns ``None`` when no handler matches or the handler
        derives nothing from ``usage``.
        """
        handler = cls.from_address(resource_address)
        if handler is None:
            return None
        return handler().derive_catalog_usage(usage, config or {})

    @classmethod
    def extract(cls, resource_address: str, resource_data: dict,
                source_format: str = "terraform") -> Optional[dict]:
        """Extract resource configuration using the appropriate handler.

        Args:
            resource_address: Full resource address
            resource_data: Raw resource data from IaC export
            source_format: "terraform", "pulumi", "cdk", or "arm"

        Returns:
            Extracted resource dict or None if unsupported.
        """
        handler = cls.from_address(resource_address)
        if not handler and source_format == "pulumi":
            # A Pulumi `id` names no resource type; the URN carries it (#396).
            handler = cls.from_address(resource_data.get("urn", ""))
        if (not handler and source_format == "terraform"
                and resource_address.startswith("module.")
                and resource_data.get("mode") != "data"
                and resource_data.get("type") and resource_data.get("name")):
            # A module's resource is addressed `module.<name>.<type>.<name>`,
            # so its own type and name select the handler.
            handler = cls.from_address(f"{resource_data['type']}.{resource_data['name']}")
        if not handler:
            return None

        extract_methods = {
            "terraform": "extract_tf",
            "pulumi": "extract_pulumi",
            "cdk": "extract_cdk",
            "arm": "extract_arm",
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
ResourceRegistry.register(ElasticIP)
ResourceRegistry.register(RDSInstance)
ResourceRegistry.register(CloudWatchLogGroup)
ResourceRegistry.register(CloudWatchMetricAlarm)
ResourceRegistry.register(ECSFargateService)
ResourceRegistry.register(ApplicationLoadBalancer)
ResourceRegistry.register(BedrockModel)
ResourceRegistry.register(ExternalNode)

# GCP handlers (DP#6: multi-cloud support)
ResourceRegistry.register(CloudRun)
ResourceRegistry.register(CloudFunction)
ResourceRegistry.register(CloudFunctionGen2)
ResourceRegistry.register(CloudStorage)
ResourceRegistry.register(Firestore)

# Azure handlers (DP#6: multi-cloud support)
ResourceRegistry.register(APIManagement)
ResourceRegistry.register(AzureFunction)
ResourceRegistry.register(AppServicePlan)
ResourceRegistry.register(CosmosDB)
ResourceRegistry.register(AzureOpenAI)
ResourceRegistry.register(AzureOpenAIDeployment)
ResourceRegistry.register(AzureBlobStorage)

# AWS miscellaneous services (Secrets Manager, ECR, Route53)
ResourceRegistry.register(SecretsManagerSecret)
ResourceRegistry.register(ECRRepository)
ResourceRegistry.register(Route53Zone)

# AWS KMS
ResourceRegistry.register(KMSKey)

# AWS WAFv2
ResourceRegistry.register(WAFv2WebACL)

# AWS Data Transfer (usage-derived node, no IaC resource)
ResourceRegistry.register(DataTransferNode)


def _module_resources(module: dict) -> list:
    """The resources of a `terraform show -json` module and of its child modules.

    A plan keeps a module's resources under its own `child_modules` entry,
    with addresses such as `module.store.aws_s3_bucket.data`, so reading the
    root module alone dropped every resource a module declares.
    """
    resources = list(module.get("resources") or [])
    for child in module.get("child_modules") or []:
        if isinstance(child, dict):
            resources.extend(_module_resources(child))
    return resources


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
    resources = tf_json.get("resource", []) or _module_resources(
        (tf_json.get("values") or {}).get("root_module") or {})
    # Function Apps need the SKU of their App Service plan (#382).
    plans = service_plans_from_tf(resources)
    # OpenAI deployments run in their account's region (#371).
    accounts = cognitive_accounts_from_tf(resources)
    # Cosmos DB accounts bill the throughput of their databases (#399).
    throughput = cosmos_throughput_from_tf(resources)

    for resource in resources:
        if isinstance(resource, dict):
            addr = resource.get("address", "")
            if addr:
                extracted = ResourceRegistry.extract(
                    addr, {**resource, SERVICE_PLANS_KEY: plans,
                           COGNITIVE_ACCOUNTS_KEY: accounts,
                           COSMOS_THROUGHPUT_KEY: throughput}, "terraform")
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
    # Function Apps need the SKU of their App Service plan (#382).
    plans = service_plans_from_pulumi(resources)
    # OpenAI deployments run in their account's region (#371).
    accounts = cognitive_accounts_from_pulumi(resources)
    # Cosmos DB accounts bill the throughput of their databases (#399).
    throughput = cosmos_throughput_from_pulumi(resources)

    for resource in resources:
        if isinstance(resource, dict):
            addr = resource.get("id", "") or resource.get("name", "")
            if addr:
                extracted = ResourceRegistry.extract(
                    addr, {**resource, SERVICE_PLANS_KEY: plans,
                           COGNITIVE_ACCOUNTS_KEY: accounts,
                           COSMOS_THROUGHPUT_KEY: throughput}, "pulumi")
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


def _arm_template(arm_json: dict) -> dict:
    """The template in an ARM template file or an `az deployment` export.

    `az deployment group export` and `az group export` print the template
    itself. Some tools wrap it as ``{"template": ...}`` or, like
    `az deployment group show`, as ``{"properties": {"template": ...}}``.
    """
    if isinstance(arm_json.get("template"), dict):
        return arm_json["template"]
    template = (arm_json.get("properties") or {}).get("template")
    if isinstance(template, dict):
        return template
    return arm_json


def _arm_resources(resources, parameters: dict, parent_type: str = "",
                   parent_name: str = ""):
    """Yield ``(address, resource)`` for each resource, nested ones included.

    ``resources`` is a list, or an object keyed by symbolic name in a
    ``languageVersion`` 2.0 template. A nested child may give a short type
    and name (``slots`` / ``staging``), which take the parent's as a prefix.
    """
    if isinstance(resources, dict):
        resources = list(resources.values())
    if not isinstance(resources, list):
        return
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        arm_type = resource.get("type", "")
        name, _ = resolve_arm_value(resource.get("name", ""), parameters)
        name = name if isinstance(name, str) else resource.get("name", "")
        if parent_type and arm_type and "/" not in arm_type:
            arm_type = f"{parent_type}/{arm_type}"
            name = f"{parent_name}/{name}"
        if not arm_type:
            continue
        yield f"{arm_type}:{name}", resource
        yield from _arm_resources(resource.get("resources"), parameters, arm_type, name)


def extract_resources_from_arm(arm_json: dict) -> dict[str, dict]:
    """Extract all resources from an Azure Resource Manager (ARM) template.

    Reads ``resources`` from an ARM template or an `az deployment` export,
    including the ``resources`` nested in a parent. Each address is the
    resource type and name joined by ``:``, as in
    ``Microsoft.Web/sites:func-orders``. A plain ``[parameters('x')]`` name
    resolves to the parameter's default value.

    Args:
        arm_json: ARM template JSON

    Returns:
        Dict mapping resource addresses to extracted configs.

    Emits UserWarning if any resources could not be extracted because
    no handler was registered for their resource type.
    """
    results = {}
    unsupported: list[str] = []
    template = _arm_template(arm_json)
    parameters = template.get("parameters") or {}

    resources = [
        (addr, {**resource, ARM_ADDRESS_KEY: addr, ARM_PARAMETERS_KEY: parameters})
        for addr, resource in _arm_resources(template.get("resources"), parameters)
    ]
    # Function Apps need the SKU of their App Service plan (#382).
    plans = service_plans_from_arm(resources)
    # OpenAI deployments run in their account's region (#371).
    accounts = cognitive_accounts_from_arm(resources)
    # Cosmos DB accounts bill the throughput of their databases (#399).
    throughput = cosmos_throughput_from_arm(resources)

    for addr, resource in resources:
        resource_data = {**resource, SERVICE_PLANS_KEY: plans,
                         COGNITIVE_ACCOUNTS_KEY: accounts,
                         COSMOS_THROUGHPUT_KEY: throughput}
        extracted = ResourceRegistry.extract(addr, resource_data, "arm")
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


def vocabulary_warnings(model: dict) -> list[str]:
    """One advisory per usage metric a node's handler doesn't declare (#427).

    The engine still prices a metric named after a catalog row, so these
    don't make a model invalid. They tell a model written before the
    handlers' logical vocabulary which name replaces each metric.
    """
    messages = []
    for address, node in (model.get("nodes") or {}).items():
        if not isinstance(node, dict):
            continue
        resource_address = node.get("resourceAddress")
        if not isinstance(resource_address, str):
            continue
        unknown = ResourceRegistry.unknown_metrics(
            resource_address, list(node.get("usageMetrics") or {}))
        if not unknown:
            continue
        allowed = ", ".join(ResourceRegistry.valid_metrics_for(resource_address))
        for name in unknown:
            messages.append(
                f"Node '{address}': usage metric '{name}' is not one of its "
                f"handler's metrics ({allowed}). A catalog row of that name still "
                f"prices it, but the handler's own name is the stable one.")
    return messages


def known_node_types() -> list[str]:
    """Return list of known node types."""
    return ["compute", "storage", "routing", "external"]


def is_leaf_node(node_type: str) -> bool:
    """Check if a node type is a leaf (cannot have outgoing edges)."""
    return node_type in ("storage", "external")