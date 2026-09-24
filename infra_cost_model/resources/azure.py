"""Azure resource handlers.

Per DP#6, the cost model supports multi-cloud. These handlers use the same
from_address / extract pattern as the AWS ones. Their catalog metrics name
the Azure rows of the seed file, which prices one region, eastus (#363).
"""

import math
import re
import warnings
from dataclasses import dataclass
from typing import Any, Optional

from .types import (
    ComputeResource, DerivedCatalogUsage, StorageResource, RoutingResource, ResourceExtract,
)


# Keys that `extract_resources_from_arm` adds to each ARM resource before it
# calls `extract_arm`. ARM resources never use them.
ARM_ADDRESS_KEY = "_address"
ARM_PARAMETERS_KEY = "_templateParameters"

# Key that the `extract_resources_from_*` functions add to each resource: the
# App Service plans of the input, from `service_plans_from_*` (#382).
SERVICE_PLANS_KEY = "_servicePlans"

# The issue that tracks pricing the plans other than consumption.
_PLAN_PRICING_ISSUE = "#383"

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


@dataclass(frozen=True)
class ServicePlan:
    """An App Service plan in the input: its resource ID, name and SKU."""
    id: Optional[str]
    name: Optional[str]
    sku: Optional[str]
    tier: Optional[str]


# hostingPlan -> (service of the Function App node, label in warnings)
_HOSTING_PLANS = {
    "consumption": ("AzureFunctions", "consumption"),
    "premium": ("AzureFunctionsPremium", "Elastic Premium"),
    "flexConsumption": ("AzureFunctionsFlexConsumption", "Flex Consumption"),
    "dedicated": ("AppService", "dedicated App Service"),
}


def hosting_plan(sku: Any, tier: Any) -> Optional[str]:
    """The hosting plan of an App Service plan SKU (#382).

    ``Y1`` or tier ``Dynamic`` is the consumption plan, ``EP1`` to ``EP3``
    or tier ``ElasticPremium`` is Elastic Premium, ``FC1`` or tier
    ``FlexConsumption`` is Flex Consumption. Any other SKU is a dedicated
    plan. Gives ``None`` when both are unknown.
    """
    sku = sku.strip().lower() if isinstance(sku, str) else ""
    tier = tier.strip().lower() if isinstance(tier, str) else ""
    if tier == "dynamic" or sku == "y1":
        return "consumption"
    if tier == "flexconsumption" or sku.startswith("fc"):
        return "flexConsumption"
    if tier == "elasticpremium" or sku.startswith("ep"):
        return "premium"
    if sku or tier:
        return "dedicated"
    return None


def _last_segment(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    return value.rstrip("/").rsplit("/", 1)[-1]


def find_service_plan(plans: list, plan_ref: Any) -> Optional[ServicePlan]:
    """The plan that ``plan_ref``, a resource ID or a name, points to.

    Tries the resource ID first. When no plan has that ID, as in a
    Terraform plan where IDs are still unknown, it tries the last segment of
    the reference against the plan names, and keeps a match only if it is
    the only one. Resource IDs and names are case-insensitive.
    """
    if not isinstance(plan_ref, str) or not plan_ref:
        return None
    ref = plan_ref.lower()
    for plan in plans:
        if plan.id and plan.id.lower() == ref:
            return plan
    name = _last_segment(ref)
    matches = [plan for plan in plans if plan.name and plan.name.lower() == name]
    return matches[0] if len(matches) == 1 else None


_SERVER_FARM_RESOURCE_ID = re.compile(
    r"^\[\s*resourceId\(\s*'microsoft\.web/serverfarms'\s*,\s*(.+?)\s*\)\s*\]$",
    re.IGNORECASE)
_STRING_LITERAL = re.compile(r"^'([^']*)'$")


def resolve_arm_server_farm(value: Any, parameters: dict) -> Optional[str]:
    """The plan a ``serverFarmId`` points to, as a resource ID or a name.

    Resolves a literal, a ``[parameters('x')]`` with a literal default, or
    ``[resourceId('Microsoft.Web/serverfarms', name)]`` whose name is a
    literal or such a parameter. Gives ``None`` for anything else.
    """
    resolved, ok = resolve_arm_value(value, parameters)
    if ok:
        return resolved if isinstance(resolved, str) else None
    match = _SERVER_FARM_RESOURCE_ID.match(value)
    if not match:
        return None
    argument = match.group(1)
    literal = _STRING_LITERAL.match(argument)
    if literal:
        return literal.group(1)
    name, ok = resolve_arm_value(f"[{argument}]", parameters)
    return name if ok and isinstance(name, str) else None


def service_plans_from_arm(resources) -> list:
    """The ``Microsoft.Web/serverfarms`` of ``(address, resource)`` pairs."""
    plans = []
    for address, resource in resources:
        arm_type, _, name = address.partition(":")
        if arm_type.lower() != "microsoft.web/serverfarms":
            continue
        parameters = resource.get(ARM_PARAMETERS_KEY, {})
        sku = resource.get("sku") or {}
        plans.append(ServicePlan(
            id=None, name=name,
            sku=resolve_arm_value(sku.get("name"), parameters)[0],
            tier=resolve_arm_value(sku.get("tier"), parameters)[0],
        ))
    return plans


def _first_block(value: Any) -> dict:
    # Terraform JSON gives a nested block as a list of one object.
    if isinstance(value, list):
        value = value[0] if value else {}
    return value if isinstance(value, dict) else {}


def service_plans_from_tf(resources: list) -> list:
    """The ``azurerm_service_plan`` and ``azurerm_app_service_plan`` resources."""
    plans = []
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        values = resource.get("values") or {}
        if resource.get("type") == "azurerm_service_plan":
            sku, tier = values.get("sku_name"), None
        elif resource.get("type") == "azurerm_app_service_plan":
            block = _first_block(values.get("sku"))
            sku, tier = block.get("size"), block.get("tier")
        else:
            continue
        plans.append(ServicePlan(id=values.get("id"), name=values.get("name"),
                                 sku=sku, tier=tier))
    return plans


def service_plans_from_pulumi(resources: list) -> list:
    """The App Service plans of a Pulumi stack export.

    Covers ``azure-native:web:AppServicePlan`` and the classic
    ``azure:appservice`` ``ServicePlan`` and ``Plan``.
    """
    plans = []
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        resource_type = resource.get("type", "")
        properties = {**(resource.get("outputs") or {}), **(resource.get("inputs") or {})}
        if resource_type == "azure-native:web:AppServicePlan":
            block = properties.get("sku") or {}
            sku, tier = block.get("name"), block.get("tier")
        elif resource_type == "azure:appservice/servicePlan:ServicePlan":
            sku, tier = properties.get("skuName"), None
        elif resource_type == "azure:appservice/plan:Plan":
            block = properties.get("sku") or {}
            sku, tier = block.get("size"), block.get("tier")
        else:
            continue
        plans.append(ServicePlan(
            id=resource.get("id"),
            name=properties.get("name") or _last_segment(resource.get("id")),
            sku=sku, tier=tier,
        ))
    return plans


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

    @staticmethod
    def hosting(address: str, plan_ref: Any, plans: list) -> tuple[str, dict]:
        """The node's service, and the hostingPlan and planSku config (#382).

        Only the consumption plan has catalog rows. An app on another plan
        gets that plan's service, so the engine reports its usage as
        unpriced, and a UserWarning. An app whose plan isn't in the input
        is priced as a consumption plan app, with a UserWarning.
        """
        plan = find_service_plan(plans, plan_ref)
        kind = hosting_plan(plan.sku, plan.tier) if plan else None
        if kind is None:
            warnings.warn(
                f"{address}: can't find the App Service plan {plan_ref!r} of this "
                f"Function App or its SKU in the input, so it is priced as a "
                f"consumption plan app. Include the plan in the input to price it "
                f"on its own plan."
            )
            return "AzureFunctions", {"hostingPlan": None, "planSku": None}
        service, label = _HOSTING_PLANS[kind]
        sku = plan.sku or plan.tier
        if kind != "consumption":
            warnings.warn(
                f"{address}: runs on the {label} plan {sku}, which the engine "
                f"doesn't price yet ({_PLAN_PRICING_ISSUE}). Its node has service "
                f"{service}, so the engine reports its usage as unpriced instead "
                f"of pricing it at consumption plan rates."
            )
        return service, {"hostingPlan": kind, "planSku": sku}

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        address = resource.get("address", "")
        # `azurerm_function_app`, the older type, names it `app_service_plan_id`.
        plan_ref = values.get("service_plan_id", values.get("app_service_plan_id"))
        service, plan = cls.hosting(address, plan_ref, resource.get(SERVICE_PLANS_KEY, []))
        return ResourceExtract(
            resource_address=address,
            node_type="compute",
            provider="azure",
            service=service,
            region=values.get("location"),
            config={
                "sku": plan_ref,
                "runtime": values.get("app_settings", {}).get("FUNCTIONS_WORKER_RUNTIME"),
                **plan,
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
        address = resource.get("id", "")
        # azure-native names the plan `serverFarmId`, the classic provider
        # `servicePlanId` or, on the older FunctionApp, `appServicePlanId`.
        plan_ref = next((inputs[key] for key in ("servicePlanId", "serverFarmId", "appServicePlanId")
                         if inputs.get(key)), None)
        service, plan = cls.hosting(address, plan_ref, resource.get(SERVICE_PLANS_KEY, []))
        return ResourceExtract(
            resource_address=address,
            node_type="compute",
            provider="azure",
            service=service,
            region=inputs.get("location"),
            config={
                "sku": plan_ref,
                "runtime": inputs.get("appSettings", {}).get("FUNCTIONS_WORKER_RUNTIME"),
                **plan,
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
        address = resource.get(ARM_ADDRESS_KEY, "")
        plan_ref = resolve_arm_server_farm(properties.get("serverFarmId"),
                                           resource.get(ARM_PARAMETERS_KEY, {}))
        service, plan = cls.hosting(address, plan_ref or properties.get("serverFarmId"),
                                    resource.get(SERVICE_PLANS_KEY, []))
        return ResourceExtract(
            resource_address=address,
            node_type="compute",
            provider="azure",
            service=service,
            region=arm_region(resource),
            config={
                "sku": properties.get("serverFarmId"),
                "runtime": app_settings.get("FUNCTIONS_WORKER_RUNTIME"),
                **plan,
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

# Cognitive Services account kinds that serve the OpenAI models (#381).
# `AIServices` accounts serve them too, at the same token rates.
_OPENAI_KINDS = frozenset({"openai", "aiservices"})


def require_openai_kind(kind: Any) -> None:
    """Raise ``NotImplementedError`` unless ``kind`` names an OpenAI account.

    A Cognitive Services account of another kind, such as
    ``SpeechServices`` or ``TextAnalytics``, is another Azure AI service. The
    registry then reports it as unsupported (#381).
    """
    if not isinstance(kind, str) or kind.strip().lower() not in _OPENAI_KINDS:
        raise NotImplementedError(f"a Cognitive Services account of kind {kind!r} is not Azure OpenAI")


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
        # These types cover every Azure AI service. The address has no kind,
        # so each `extract_*` method checks it (#381).
        if (resource_address.startswith("azurerm_cognitive_account.") or
                "azure:cognitiveservices:Account:" in resource_address or
                matches_arm_type(resource_address, "Microsoft.CognitiveServices/accounts")):
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        require_openai_kind(values.get("kind"))
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
        kind = inputs.get("kind", (resource.get("outputs") or {}).get("kind"))
        require_openai_kind(kind)
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="compute",
            provider="azure",
            service="AzureOpenAI",
            region=inputs.get("location"),
            config={
                "kind": kind,
                "skuName": inputs.get("sku", {}).get("name"),
            },
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        properties = resource.get("Properties", {})
        require_openai_kind(properties.get("kind"))
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
        kind, _ = resolve_arm_value(resource.get("kind"), resource.get(ARM_PARAMETERS_KEY, {}))
        require_openai_kind(kind)
        return ResourceExtract(
            resource_address=resource.get(ARM_ADDRESS_KEY, ""),
            node_type="compute",
            provider="azure",
            service="AzureOpenAI",
            region=arm_region(resource),
            config={
                "kind": kind,
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
