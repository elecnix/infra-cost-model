"""Azure resource handlers.

Per DP#6, the cost model supports multi-cloud. These handlers use the same
from_address / extract pattern as the AWS ones. Their catalog metrics name
the Azure rows of the seed file, which prices one region, eastus (#363).
"""

import math
import re
import warnings
from typing import Any, Optional

from .types import (
    ComputeResource, DerivedCatalogUsage, StorageResource, RoutingResource, ResourceExtract,
)


# Keys that `extract_resources_from_arm` adds to each ARM resource before it
# calls `extract_arm`. ARM resources never use them.
ARM_ADDRESS_KEY = "_address"
ARM_PARAMETERS_KEY = "_templateParameters"

_PARAMETER_EXPRESSION = re.compile(r"^\[\s*parameters\(\s*'([^']+)'\s*\)\s*\]$")


def matches_arm_type(resource_address: str, arm_type: str) -> bool:
    """Whether ``resource_address`` names a resource of exactly ``arm_type``.

    Two address forms reach the Azure handlers:

    - ``{type}:{name}``, built by ``extract_resources_from_arm``. The type
      must be followed by ``:``, so a child such as
      ``Microsoft.Web/sites/slots:app/staging`` doesn't match
      ``Microsoft.Web/sites``.
    - An Azure resource ID, ``.../providers/{namespace}/{type}/{name}``, as
      found in a Pulumi stack export's ``id``. The parent type must be
      followed by exactly one name segment, so child IDs don't match.

    ARM resource types are case-insensitive.
    """
    address = resource_address.lower()
    wanted = arm_type.lower()
    if address.startswith(wanted + ":"):
        return True
    marker = "/providers/"
    index = address.rfind(marker)
    if index == -1:
        return False
    segments = address[index + len(marker):].split("/")
    # namespace, type, name: a top-level resource has exactly three segments.
    return len(segments) == 3 and segments[2] != "" and "/".join(segments[:2]) == wanted


def resolve_arm_value(value: Any, parameters: dict) -> tuple[Any, bool]:
    """Resolve an ARM template value to a literal.

    Returns ``(value, True)`` for a literal, or for a plain
    ``[parameters('x')]`` whose parameter has a literal default. Returns
    ``(None, False)`` for any other expression, which only a deployment can
    evaluate.
    """
    if not isinstance(value, str) or not value.startswith("["):
        return value, True
    if value.startswith("[["):
        # `[[` escapes a literal string that starts with `[`.
        return value[1:], True
    match = _PARAMETER_EXPRESSION.match(value)
    if match:
        parameter = parameters.get(match.group(1))
        if isinstance(parameter, dict) and "defaultValue" in parameter:
            return resolve_arm_value(parameter["defaultValue"], parameters)
    return None, False


def arm_region(resource: dict) -> Optional[str]:
    """The region of an ARM resource, from its ``location``.

    An expression that can't be resolved without a deployment, such as
    ``[resourceGroup().location]``, gives ``None`` and a UserWarning.
    """
    location = resource.get("location")
    region, resolved = resolve_arm_value(location, resource.get(ARM_PARAMETERS_KEY, {}))
    if not resolved:
        warnings.warn(
            f"{resource.get(ARM_ADDRESS_KEY, resource.get('name', '?'))}: can't resolve "
            f"location {location!r} without a deployment, so its region is unset. "
            f"Give the location as a literal or as a parameter with a default value."
        )
    return region


def _arm_properties(resource: dict) -> dict:
    return resource.get("properties") or {}


def _arm_sku_name(resource: dict) -> Optional[str]:
    return (resource.get("sku") or {}).get("name")


def is_function_app_kind(kind: Any) -> bool:
    """Whether a ``Microsoft.Web/sites`` ``kind`` names a Function App (#364).

    The kind is a comma-separated list, such as ``functionapp,linux``. A web
    app has ``app`` or ``app,linux``. A site with no kind is a web app.
    """
    if not isinstance(kind, str):
        return False
    return "functionapp" in (part.strip().lower() for part in kind.split(","))


class AzureFunction(ComputeResource):
    """Azure Function App - compute node (equivalent to AWS Lambda)."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["invocations", "avgDurationMs", "memoryMb"]

    def derive_catalog_usage(self, usage: dict[str, float]) -> Optional[DerivedCatalogUsage]:
        """Derive executions and GB-seconds, the quantities the consumption plan bills.

        Azure rounds memory up to the next 128 MB and bills at least 100 ms
        for each execution.
        """
        inputs = ("invocations", "avgDurationMs", "memoryMb")
        if not all(name in usage for name in inputs):
            return None
        invocations = usage["invocations"]
        memory_gb = math.ceil(usage["memoryMb"] / 128) * 128 / 1024
        seconds = max(usage["avgDurationMs"], 100.0) / 1000
        return DerivedCatalogUsage(
            consumed=frozenset(inputs),
            quantities={
                "AzureFunctions-Execution": invocations,
                "AzureFunctions-GB-Second": invocations * memory_gb * seconds,
            },
        )

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["AzureFunction"]:
        # `Microsoft.Web/sites` also covers web apps. The address has no
        # kind, so `extract_arm` and `extract_pulumi` check it (#364).
        # Terraform names web apps with other types, such as
        # `azurerm_linux_web_app`, which don't match.
        if (resource_address.startswith("azurerm_function_app.") or
                resource_address.startswith("azurerm_linux_function_app.") or
                resource_address.startswith("azurerm_windows_function_app.") or
                "azure:appservice:FunctionApp:" in resource_address or
                matches_arm_type(resource_address, "Microsoft.Web/sites")):
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="compute",
            provider="azure",
            service="AzureFunctions",
            region=values.get("location"),
            config={
                "sku": values.get("service_plan_id"),
                "runtime": values.get("app_settings", {}).get("FUNCTIONS_WORKER_RUNTIME"),
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        # An azure-native `WebApp` is a Function App or a web app by its kind.
        kind = inputs.get("kind", (resource.get("outputs") or {}).get("kind"))
        if (resource.get("type") == "azure-native:web:WebApp"
                and not is_function_app_kind(kind)):
            raise NotImplementedError("a web app is not a Function App")
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="compute",
            provider="azure",
            service="AzureFunctions",
            region=inputs.get("location"),
            config={
                "sku": inputs.get("servicePlanId"),
                "runtime": inputs.get("appSettings", {}).get("FUNCTIONS_WORKER_RUNTIME"),
            },
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        properties = resource.get("Properties", {})
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="compute",
            provider="azure",
            service="AzureFunctions",
            region=properties.get("location"),
            config={
                "sku": properties.get("serverFarmId"),
                "runtime": properties.get("siteConfig", {}).get("linuxFxVersion"),
            },
        )


    @classmethod
    def extract_arm(cls, resource: dict) -> ResourceExtract:
        if not is_function_app_kind(resource.get("kind")):
            # A web app: the registry reports it as unsupported.
            raise NotImplementedError("a web app is not a Function App")
        properties = _arm_properties(resource)
        app_settings = {
            setting.get("name"): setting.get("value")
            for setting in (properties.get("siteConfig") or {}).get("appSettings") or []
            if isinstance(setting, dict)
        }
        return ResourceExtract(
            resource_address=resource.get(ARM_ADDRESS_KEY, ""),
            node_type="compute",
            provider="azure",
            service="AzureFunctions",
            region=arm_region(resource),
            config={
                "sku": properties.get("serverFarmId"),
                "runtime": app_settings.get("FUNCTIONS_WORKER_RUNTIME"),
            },
        )

class CosmosDB(StorageResource):
    """Azure Cosmos DB - storage node (equivalent to DynamoDB)."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["readRequests", "writeRequests", "requestUnits", "storageGb"]

    @property
    def catalog_metrics(self) -> dict[str, str]:
        # Serverless accounts bill request units, and a read or a write costs a
        # number of them that depends on the item. So the catalog prices
        # `requestUnits`, and has no price for a read or a write.
        return {"requestUnits": "CosmosDB-Serverless-RU",
                "storageGb": "CosmosDB-Storage-GB-Month"}

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["CosmosDB"]:
        if (resource_address.startswith("azurerm_cosmosdb_account.") or
                "azure:cosmosdb:Account:" in resource_address or
                matches_arm_type(resource_address, "Microsoft.DocumentDB/databaseAccounts")):
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="storage",
            provider="azure",
            service="CosmosDB",
            region=values.get("location"),
            config={
                "offerType": values.get("offer_type"),
                "kind": values.get("kind"),
                "consistencyLevel": values.get("consistency_policy", {}).get("consistency_level"),
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="storage",
            provider="azure",
            service="CosmosDB",
            region=inputs.get("location"),
            config={
                "offerType": inputs.get("offerType"),
                "kind": inputs.get("kind"),
            },
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        properties = resource.get("Properties", {})
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="storage",
            provider="azure",
            service="CosmosDB",
            region=properties.get("location"),
            config={
                "offerType": properties.get("databaseAccountOfferType"),
                "kind": properties.get("kind"),
            },
        )


    @classmethod
    def extract_arm(cls, resource: dict) -> ResourceExtract:
        properties = _arm_properties(resource)
        return ResourceExtract(
            resource_address=resource.get(ARM_ADDRESS_KEY, ""),
            node_type="storage",
            provider="azure",
            service="CosmosDB",
            region=arm_region(resource),
            config={
                "offerType": properties.get("databaseAccountOfferType"),
                "kind": resource.get("kind"),
                "consistencyLevel": (properties.get("consistencyPolicy") or {}).get(
                    "defaultConsistencyLevel"),
            },
        )

class APIManagement(RoutingResource):
    """Azure API Management - routing node (equivalent to API Gateway)."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["requests", "dataOutGb"]

    @property
    def catalog_metrics(self) -> dict[str, str]:
        return {"requests": "APIM-Consumption-Call"}

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["APIManagement"]:
        if (resource_address.startswith("azurerm_api_management.") or
                "azure:apimanagement:Service:" in resource_address or
                matches_arm_type(resource_address, "Microsoft.ApiManagement/service")):
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="routing",
            provider="azure",
            service="APIManagement",
            region=values.get("location"),
            config={
                "skuName": values.get("sku_name"),
                "publisherName": values.get("publisher_name"),
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="routing",
            provider="azure",
            service="APIManagement",
            region=inputs.get("location"),
            config={
                "skuName": inputs.get("skuName"),
                "publisherName": inputs.get("publisherName"),
            },
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        properties = resource.get("Properties", {})
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="routing",
            provider="azure",
            service="APIManagement",
            region=properties.get("location"),
            config={
                "sku": properties.get("sku", {}).get("name"),
                "publisherEmail": properties.get("publisherEmail"),
            },
        )


    @classmethod
    def extract_arm(cls, resource: dict) -> ResourceExtract:
        return ResourceExtract(
            resource_address=resource.get(ARM_ADDRESS_KEY, ""),
            node_type="routing",
            provider="azure",
            service="APIManagement",
            region=arm_region(resource),
            config={
                "skuName": _arm_sku_name(resource),
                "publisherName": _arm_properties(resource).get("publisherName"),
            },
        )

class AzureOpenAI(ComputeResource):
    """Azure OpenAI Service - compute node (equivalent to Bedrock)."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["invocations", "inputTokens", "outputTokens"]

    @property
    def catalog_metrics(self) -> dict[str, str]:
        # The seed prices GPT-4o (2024-08-06) in a Global Standard deployment.
        return {"inputTokens": "AzureOpenAI-Input-Token",
                "outputTokens": "AzureOpenAI-Output-Token"}

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["AzureOpenAI"]:
        if (resource_address.startswith("azurerm_cognitive_account.") or
                "azure:cognitiveservices:Account:" in resource_address or
                matches_arm_type(resource_address, "Microsoft.CognitiveServices/accounts")):
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="compute",
            provider="azure",
            service="AzureOpenAI",
            region=values.get("location"),
            config={
                "kind": values.get("kind"),
                "skuName": values.get("sku_name"),
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="compute",
            provider="azure",
            service="AzureOpenAI",
            region=inputs.get("location"),
            config={
                "kind": inputs.get("kind"),
                "skuName": inputs.get("sku", {}).get("name"),
            },
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        properties = resource.get("Properties", {})
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="compute",
            provider="azure",
            service="AzureOpenAI",
            region=properties.get("location"),
            config={
                "kind": properties.get("kind"),
                "sku": properties.get("sku", {}).get("name"),
            },
        )


    @classmethod
    def extract_arm(cls, resource: dict) -> ResourceExtract:
        return ResourceExtract(
            resource_address=resource.get(ARM_ADDRESS_KEY, ""),
            node_type="compute",
            provider="azure",
            service="AzureOpenAI",
            region=arm_region(resource),
            config={
                "kind": resource.get("kind"),
                "skuName": _arm_sku_name(resource),
            },
        )

class AzureBlobStorage(StorageResource):
    """Azure Blob Storage - storage node (equivalent to S3)."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["storageGb", "readRequests", "writeRequests", "dataOutGb"]

    @property
    def catalog_metrics(self) -> dict[str, str]:
        # Hot tier with LRS, the defaults of azurerm_storage_account.
        return {"storageGb": "Blob-Hot-LRS-GB-Month",
                "readRequests": "Blob-Hot-Read-Operation",
                "writeRequests": "Blob-Hot-LRS-Write-Operation"}

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["AzureBlobStorage"]:
        if (resource_address.startswith("azurerm_storage_account.") or
                "azure:storage:Account:" in resource_address or
                matches_arm_type(resource_address, "Microsoft.Storage/storageAccounts")):
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="storage",
            provider="azure",
            service="BlobStorage",
            region=values.get("location"),
            config={
                "accountTier": values.get("account_tier"),
                "replicationType": values.get("account_replication_type"),
                "accessTier": values.get("access_tier"),
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="storage",
            provider="azure",
            service="BlobStorage",
            region=inputs.get("location"),
            config={
                "accountTier": inputs.get("accountTier"),
                "replicationType": inputs.get("accountReplicationType"),
            },
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        properties = resource.get("Properties", {})
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="storage",
            provider="azure",
            service="BlobStorage",
            region=properties.get("location"),
            config={
                "accountTier": properties.get("sku", {}).get("name"),
                "accessTier": properties.get("accessTier"),
            },
        )

    @classmethod
    def extract_arm(cls, resource: dict) -> ResourceExtract:
        # An ARM storage SKU joins tier and replication, as in `Standard_LRS`.
        tier, _, replication = (_arm_sku_name(resource) or "").partition("_")
        return ResourceExtract(
            resource_address=resource.get(ARM_ADDRESS_KEY, ""),
            node_type="storage",
            provider="azure",
            service="BlobStorage",
            region=arm_region(resource),
            config={
                "accountTier": tier or None,
                "replicationType": replication or None,
                "accessTier": _arm_properties(resource).get("accessTier"),
            },
        )
