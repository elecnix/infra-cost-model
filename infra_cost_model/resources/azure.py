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

# Azure bills every service's internet egress on one Bandwidth meter, so the
# handlers price `dataOutGb` from the same rows and share its tiers (#372).
_EGRESS_SERVICE = "Bandwidth"
_EGRESS_METRIC = "Bandwidth-Internet-Out-GB"

_PARAMETER_EXPRESSION = re.compile(r"^\[\s*parameters\(\s*'([^']+)'\s*\)\s*\]$")


def matches_arm_type(resource_address: str, arm_type: str) -> bool:
    """Whether ``resource_address`` names a resource of exactly ``arm_type``.

    Two address forms reach the Azure handlers:

    - ``{type}:{name}``, built by ``extract_resources_from_arm``. The type
      must be followed by ``:``, so a child such as
      ``Microsoft.Web/sites/slots:app/staging`` doesn't match
      ``Microsoft.Web/sites``.
    - An Azure resource ID, ``.../providers/{namespace}/{type}/{name}``, as
      found in a Pulumi stack export's ``id``. Each type must be followed by
      exactly one name segment, so child IDs don't match their parent's
      type. A child type, such as
      ``Microsoft.CognitiveServices/accounts/deployments``, matches
      ``.../accounts/{name}/deployments/{name}``.

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
    namespace, *types = wanted.split("/")
    # The namespace, then a type and a name for each level.
    if len(segments) != 1 + 2 * len(types) or segments[0] != namespace:
        return False
    return all(segments[1 + 2 * i] == t and segments[2 + 2 * i] != ""
               for i, t in enumerate(types))


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
    if tier == "flexconsumption" or sku == "fc1":
        return "flexConsumption"
    if tier == "elasticpremium" or sku in ("ep1", "ep2", "ep3"):
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

    def derive_catalog_usage(self, usage: dict[str, float],
                             config: Optional[dict] = None) -> Optional[DerivedCatalogUsage]:
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
        """The node's service, and its hostingPlan, planSku and planTier config (#382).

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
            return "AzureFunctions", {"hostingPlan": None, "planSku": None, "planTier": None}
        service, label = _HOSTING_PLANS[kind]
        if kind != "consumption":
            warnings.warn(
                f"{address}: runs on the {label} plan {plan.sku or plan.tier}, which the engine "
                f"doesn't price yet ({_PLAN_PRICING_ISSUE}). Its node has service "
                f"{service}, so the engine reports its usage as unpriced instead "
                f"of pricing it at consumption plan rates."
            )
        return service, {"hostingPlan": kind, "planSku": plan.sku, "planTier": plan.tier}

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
        if resource.get("type", "").startswith("azure-native:"):
            plan_keys = ("serverFarmId",)
        else:
            plan_keys = ("servicePlanId", "appServicePlanId")
        plan_ref = next((inputs[key] for key in plan_keys if inputs.get(key)), None)
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

def _capabilities(value: Any) -> set:
    """The capability names of a Cosmos DB account, lower case."""
    names = set()
    for item in value or []:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str):
            names.add(name.strip().lower())
    return names


def cosmos_capacity_mode(capabilities: Any, capacity_mode: Any = None) -> str:
    """``serverless`` or ``provisioned``, from the account's settings (#375).

    An account is serverless when ``capacityMode`` says so, or when it has
    the ``EnableServerless`` capability. Any other account has provisioned
    throughput.
    """
    if isinstance(capacity_mode, str) and capacity_mode.strip().lower() == "serverless":
        return "serverless"
    return "serverless" if "enableserverless" in _capabilities(capabilities) else "provisioned"


# Request units per operation on a 1 KB item, from "Request Units in Azure
# Cosmos DB" (https://learn.microsoft.com/azure/cosmos-db/request-units): a
# point read costs 1 RU, and a write about 5 RU with the default indexing
# policy. A node's `config` can set `ruPerRead` and `ruPerWrite` (#374).
DEFAULT_RU_PER_READ = 1.0
DEFAULT_RU_PER_WRITE = 5.0


class CosmosDB(StorageResource):
    """Azure Cosmos DB - storage node (equivalent to DynamoDB).

    A serverless account bills request units. A provisioned account bills
    hours of 100 RU/s of throughput (the ``throughputHours`` metric, which is
    usually fixed), and its reads and writes cost nothing more (#375).
    """

    @property
    def valid_metrics(self) -> list[str]:
        return ["readRequests", "writeRequests", "requestUnits", "storageGb",
                "throughputHours"]

    @property
    def catalog_metrics(self) -> dict[str, str]:
        # Serverless accounts bill request units, and a read or a write costs a
        # number of them that depends on the item. So the catalog prices
        # `requestUnits`, and `derive_catalog_usage` turns reads and writes
        # into request units (#374).
        return {"requestUnits": "CosmosDB-Serverless-RU",
                "storageGb": "CosmosDB-Storage-GB-Month"}

    def catalog_metrics_for(self, config: dict) -> dict[str, str]:
        config = config or {}
        if (config.get("capacityMode") or "serverless") == "serverless":
            return self.catalog_metrics
        throughput = ("CosmosDB-Provisioned-MultiRegionWrite-100RU-Hour"
                      if config.get("multiRegionWrites") else "CosmosDB-Provisioned-100RU-Hour")
        return {"storageGb": "CosmosDB-Storage-GB-Month", "throughputHours": throughput}

    def derive_catalog_usage(self, usage: dict[str, float],
                             config: Optional[dict] = None) -> Optional[DerivedCatalogUsage]:
        """Request units from reads, writes and ``requestUnits`` (#374).

        A read costs ``ruPerRead`` request units and a write ``ruPerWrite``,
        from the node's ``config``, by default the figures for a 1 KB item.
        On a provisioned account, the throughput pays for every operation,
        so they derive no quantity.
        """
        config = config or {}
        inputs = [name for name in ("readRequests", "writeRequests", "requestUnits")
                  if name in usage]
        if not inputs:
            return None
        if (config.get("capacityMode") or "serverless") != "serverless":
            return DerivedCatalogUsage(consumed=frozenset(inputs), quantities={})
        per_read = float(config.get("ruPerRead", DEFAULT_RU_PER_READ))
        per_write = float(config.get("ruPerWrite", DEFAULT_RU_PER_WRITE))
        request_units = (usage.get("readRequests", 0.0) * per_read
                         + usage.get("writeRequests", 0.0) * per_write
                         + usage.get("requestUnits", 0.0))
        return DerivedCatalogUsage(consumed=frozenset(inputs),
                                   quantities={"CosmosDB-Serverless-RU": request_units})

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
        # azurerm 4.x names it `multiple_write_locations_enabled`, 3.x
        # `enable_multiple_write_locations`.
        multi_region = values.get("multiple_write_locations_enabled",
                                  values.get("enable_multiple_write_locations"))
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="storage",
            provider="azure",
            service="CosmosDB",
            region=values.get("location"),
            config={
                "offerType": values.get("offer_type"),
                "kind": values.get("kind"),
                "consistencyLevel": _first_block(values.get("consistency_policy")).get(
                    "consistency_level"),
                "capacityMode": cosmos_capacity_mode(values.get("capabilities")),
                "multiRegionWrites": bool(multi_region),
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        properties = inputs.get("properties") or {}
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="storage",
            provider="azure",
            service="CosmosDB",
            region=inputs.get("location"),
            config={
                "offerType": inputs.get("offerType", inputs.get("databaseAccountOfferType")),
                "kind": inputs.get("kind"),
                "capacityMode": cosmos_capacity_mode(
                    inputs.get("capabilities", properties.get("capabilities")),
                    inputs.get("capacityMode")),
                "multiRegionWrites": bool(inputs.get("enableMultipleWriteLocations")),
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
                "capacityMode": cosmos_capacity_mode(properties.get("capabilities"),
                                                     properties.get("capacityMode")),
                "multiRegionWrites": bool(properties.get("enableMultipleWriteLocations")),
            },
        )


# API Management tiers (#375). The consumption tier bills calls. The other
# tiers bill unit-hours (the `unitHours` metric, usually fixed). The v2
# tiers also bill the calls over a monthly allowance. The classic tiers
# include every call.
_APIM_TIERS = {
    "consumption": "Consumption", "developer": "Developer", "basic": "Basic",
    "standard": "Standard", "premium": "Premium", "isolated": "Isolated",
    "basicv2": "BasicV2", "standardv2": "StandardV2", "premiumv2": "PremiumV2",
}
_APIM_V2_CALL_TIERS = ("BasicV2", "StandardV2")


def apim_tier(sku_name: Any) -> Optional[str]:
    """The tier of an API Management SKU such as ``Developer_1``.

    Gives ``None`` when the SKU is unset, and the SKU's own name when the
    tier is unknown.
    """
    if not isinstance(sku_name, str) or not sku_name.strip():
        return None
    name = sku_name.strip().split("_", 1)[0]
    return _APIM_TIERS.get(name.lower(), name)


class APIManagement(RoutingResource):
    """Azure API Management - routing node (equivalent to API Gateway)."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["requests", "dataOutGb", "unitHours"]

    @property
    def catalog_metrics(self) -> dict[str, str]:
        return {"requests": "APIM-Consumption-Call", "dataOutGb": _EGRESS_METRIC}

    @property
    def catalog_services(self) -> dict[str, str]:
        return {_EGRESS_METRIC: _EGRESS_SERVICE}

    def catalog_metrics_for(self, config: dict) -> dict[str, str]:
        tier = apim_tier((config or {}).get("skuName")) or "Consumption"
        metrics = dict(self.catalog_metrics)
        if tier == "Consumption":
            return metrics
        del metrics["requests"]
        metrics["unitHours"] = f"APIM-{tier}-Unit-Hour"
        if tier in _APIM_V2_CALL_TIERS:
            metrics["requests"] = f"APIM-{tier}-Call"
        return metrics

    def derive_catalog_usage(self, usage: dict[str, float],
                             config: Optional[dict] = None) -> Optional[DerivedCatalogUsage]:
        """The classic tiers include every call, so their calls cost nothing."""
        tier = apim_tier((config or {}).get("skuName")) or "Consumption"
        if ("requests" not in usage or tier == "Consumption"
                or tier in _APIM_V2_CALL_TIERS or tier not in _APIM_TIERS.values()):
            return None
        return DerivedCatalogUsage(consumed=frozenset({"requests"}), quantities={})

    @staticmethod
    def _warn_unknown_tier(address: str, sku_name: Any) -> None:
        tier = apim_tier(sku_name)
        if tier is not None and tier not in _APIM_TIERS.values():
            warnings.warn(
                f"{address}: API Management SKU {sku_name!r} names a tier that has no "
                f"catalog rows, so the engine reports its usage as unpriced. The "
                f"catalog prices {', '.join(sorted(_APIM_TIERS.values()))}."
            )

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
        address = resource.get("address", "")
        cls._warn_unknown_tier(address, values.get("sku_name"))
        return ResourceExtract(
            resource_address=address,
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
        address = resource.get("id", "")
        sku_name = inputs.get("skuName")
        if sku_name is None and isinstance(inputs.get("sku"), dict):
            # azure-native gives the SKU as {name, capacity}.
            sku = inputs["sku"]
            sku_name = f"{sku.get('name')}_{sku.get('capacity', 1)}" if sku.get("name") else None
        cls._warn_unknown_tier(address, sku_name)
        return ResourceExtract(
            resource_address=address,
            node_type="routing",
            provider="azure",
            service="APIManagement",
            region=inputs.get("location"),
            config={
                "skuName": sku_name,
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
        address = resource.get(ARM_ADDRESS_KEY, "")
        parameters = resource.get(ARM_PARAMETERS_KEY, {})
        sku = resource.get("sku") or {}
        name, _ = resolve_arm_value(sku.get("name"), parameters)
        capacity, _ = resolve_arm_value(sku.get("capacity", 1), parameters)
        # ARM gives the tier and the unit count apart, as in Terraform's
        # `Developer_1`.
        sku_name = f"{name}_{capacity}" if isinstance(name, str) else None
        cls._warn_unknown_tier(address, sku_name)
        return ResourceExtract(
            resource_address=address,
            node_type="routing",
            provider="azure",
            service="APIManagement",
            region=arm_region(resource),
            config={
                "skuName": sku_name,
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


# Deployment types with token prices, and the part of the catalog metric name
# that names each one (#371). Azure prices tokens per model and per
# deployment type. Other types, such as the provisioned ones (billed per
# unit-hour) and the batch ones, have no rows.
_OPENAI_DEPLOYMENT_TIERS = {
    "globalstandard": "Global",
    "datazonestandard": "DataZone",
    "standard": "Regional",
}
_ALL_TIERS = ("GlobalStandard", "DataZoneStandard", "Standard")


@dataclass(frozen=True)
class OpenAIModel:
    """A model whose token prices are in the seed catalog.

    ``versions`` are the model versions those prices cover. ``tiers`` are
    the deployment types with rows. ``output`` is false for an embedding
    model, which bills input tokens only.
    """
    versions: tuple
    tiers: tuple = _ALL_TIERS
    output: bool = True


# Models with seed rows for eastus, from the Azure Retail Prices API (#371).
OPENAI_MODELS = {
    # 2024-11-20 has the same prices as 2024-08-06. 2024-05-13 costs more.
    "gpt-4o": OpenAIModel(versions=("2024-08-06", "2024-11-20")),
    "gpt-4o-mini": OpenAIModel(versions=("2024-07-18",)),
    "gpt-4.1": OpenAIModel(versions=("2025-04-14",)),
    "gpt-4.1-mini": OpenAIModel(versions=("2025-04-14",)),
    "gpt-4.1-nano": OpenAIModel(versions=("2025-04-14",)),
    "o3": OpenAIModel(versions=("2025-04-16",)),
    "o3-mini": OpenAIModel(versions=("2025-01-31",)),
    "text-embedding-3-small": OpenAIModel(
        versions=("1",), tiers=("GlobalStandard", "Standard"), output=False),
    "text-embedding-3-large": OpenAIModel(
        versions=("1",), tiers=("GlobalStandard", "Standard"), output=False),
}

# A node that names no model is priced as GPT-4o in a Global Standard
# deployment, the rows the seed had before #371.
_DEFAULT_OPENAI_MODEL = "gpt-4o"
_DEFAULT_DEPLOYMENT_TYPE = "GlobalStandard"


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def openai_catalog_product(config: dict) -> tuple[str, str]:
    """The catalog model and tier of a node's ``config`` (#371).

    ``config`` may name ``model``, ``modelVersion`` and ``deploymentType``
    (the deployment SKU, such as ``GlobalStandard``). A version whose
    prices differ from the seeded ones gets a model name of its own, such
    as ``gpt-4o-2024-05-13``, which has no rows, so the engine reports its
    tokens as unpriced instead of pricing them at another version's rates.
    """
    model = (_text(config.get("model")) or _DEFAULT_OPENAI_MODEL).lower()
    version = _text(config.get("modelVersion"))
    known = OPENAI_MODELS.get(model)
    if known is not None and version is not None and version not in known.versions:
        model = f"{model}-{version}"
    deployment_type = _text(config.get("deploymentType")) or _DEFAULT_DEPLOYMENT_TYPE
    tier = _OPENAI_DEPLOYMENT_TIERS.get(deployment_type.lower(), deployment_type)
    return model, tier


def openai_pricing_warning(address: str, config: dict) -> Optional[str]:
    """Why the catalog has no token prices for ``config``, or ``None``."""
    model = _text(config.get("model")) or _DEFAULT_OPENAI_MODEL
    version = _text(config.get("modelVersion"))
    deployment_type = _text(config.get("deploymentType")) or _DEFAULT_DEPLOYMENT_TYPE
    known = OPENAI_MODELS.get(model.lower())
    if known is None or (version is not None and version not in known.versions):
        label = f"{model} {version}" if version else model
        return (f"{address}: model {label!r} has no catalog rows, so the engine "
                f"reports its tokens as unpriced. The catalog prices "
                f"{', '.join(sorted(OPENAI_MODELS))}.")
    if deployment_type.lower() not in (t.lower() for t in known.tiers):
        return (f"{address}: deployment type {deployment_type!r} of {model} has no "
                f"token prices in the catalog, so the engine reports its tokens as "
                f"unpriced. The catalog prices {', '.join(known.tiers)}.")
    return None


class AzureOpenAI(ComputeResource):
    """Azure OpenAI Service - compute node (equivalent to Bedrock)."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["invocations", "inputTokens", "outputTokens", "cachedReadTokens"]

    @property
    def catalog_metrics(self) -> dict[str, str]:
        return self.catalog_metrics_for({})

    def catalog_metrics_for(self, config: dict) -> dict[str, str]:
        """The token rows of the node's model and deployment type (#371)."""
        model, tier = openai_catalog_product(config or {})
        prefix = f"AzureOpenAI-{model}-{tier}"
        return {"inputTokens": f"{prefix}-Input-Token",
                "cachedReadTokens": f"{prefix}-Cached-Input-Token",
                "outputTokens": f"{prefix}-Output-Token"}

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


@dataclass(frozen=True)
class CognitiveAccount:
    """A Cognitive Services account in the input: its resource ID, name and region."""
    id: Optional[str]
    name: Optional[str]
    region: Optional[str]


def cognitive_accounts_from_arm(resources) -> list:
    """The ``Microsoft.CognitiveServices/accounts`` of ``(address, resource)`` pairs."""
    accounts = []
    for address, resource in resources:
        arm_type, _, name = address.partition(":")
        if arm_type.lower() != "microsoft.cognitiveservices/accounts":
            continue
        region, _ = resolve_arm_value(resource.get("location"),
                                      resource.get(ARM_PARAMETERS_KEY, {}))
        accounts.append(CognitiveAccount(id=None, name=name, region=region))
    return accounts


def cognitive_accounts_from_tf(resources: list) -> list:
    """The ``azurerm_cognitive_account`` resources."""
    return [
        CognitiveAccount(id=values.get("id"), name=values.get("name"),
                         region=values.get("location"))
        for resource in resources
        if isinstance(resource, dict) and resource.get("type") == "azurerm_cognitive_account"
        for values in [resource.get("values") or {}]
    ]


def cognitive_accounts_from_pulumi(resources: list) -> list:
    """The Cognitive Services accounts of a Pulumi stack export."""
    accounts = []
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        inputs = resource.get("inputs") or {}
        resource_type = resource.get("type", "")
        if resource_type == "azure-native:cognitiveservices:Account":
            name = inputs.get("accountName")
        elif resource_type == "azure:cognitive/account:Account":
            name = inputs.get("name")
        else:
            continue
        accounts.append(CognitiveAccount(
            id=resource.get("id"), name=name or _last_segment(resource.get("id")),
            region=inputs.get("location")))
    return accounts


# Key that the `extract_resources_from_*` functions add to each resource: the
# Cognitive Services accounts of the input, whose region a deployment has (#371).
COGNITIVE_ACCOUNTS_KEY = "_cognitiveAccounts"

_DEPLOYMENT_ID = re.compile(r"/accounts/([^/]+)/deployments/[^/]+$", re.IGNORECASE)


class AzureOpenAIDeployment(AzureOpenAI):
    """A model deployment of an Azure OpenAI account (#371).

    Azure bills tokens per deployment, at the prices of its model and
    deployment type. The node's ``config`` names them, so the handler picks
    their catalog rows. A deployment has no location: it runs in its
    account's region.
    """

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["AzureOpenAIDeployment"]:
        if (resource_address.startswith("azurerm_cognitive_deployment.") or
                matches_arm_type(resource_address,
                                 "Microsoft.CognitiveServices/accounts/deployments")):
            return cls()
        return None

    @staticmethod
    def _extract(address: str, resource: dict, account_ref: Any, model: dict,
                 deployment_type: Any) -> ResourceExtract:
        accounts = resource.get(COGNITIVE_ACCOUNTS_KEY, [])
        account = find_service_plan(accounts, account_ref)
        if account is None and account_ref is None and len(accounts) == 1:
            # A Terraform plan doesn't know the account ID before apply. A
            # reference that matches no account is another account, so it
            # gets the warning below.
            account = accounts[0]
        if account is None:
            warnings.warn(
                f"{address}: can't find the account {account_ref!r} of this "
                f"deployment in the input, so its region is unset. Include the "
                f"account in the input, or set the node's region."
            )
        config = {
            "model": _text(model.get("name")),
            "modelVersion": _text(model.get("version")),
            "deploymentType": _text(deployment_type),
            "account": account_ref if account_ref is not None else (account and account.id),
        }
        warning = openai_pricing_warning(address, config)
        if warning:
            warnings.warn(warning)
        return ResourceExtract(
            resource_address=address,
            node_type="compute",
            provider="azure",
            service="AzureOpenAI",
            region=account.region if account else None,
            config=config,
        )

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        # azurerm 4.x names the SKU in `sku`, 3.x in `scale`.
        sku = _first_block(values.get("sku")).get("name") or \
            _first_block(values.get("scale")).get("type")
        return cls._extract(resource.get("address", ""), resource,
                            values.get("cognitive_account_id"),
                            _first_block(values.get("model")), sku)

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        address = resource.get("id", "")
        if resource.get("type", "").startswith("azure-native:"):
            account_ref = inputs.get("accountName")
            model = (inputs.get("properties") or {}).get("model") or {}
        else:
            account_ref = inputs.get("cognitiveAccountId")
            model = inputs.get("model") or {}
        if not account_ref:
            match = _DEPLOYMENT_ID.search(address)
            account_ref = match.group(1) if match else None
        return cls._extract(address, resource, account_ref, model,
                            (inputs.get("sku") or {}).get("name"))

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        raise NotImplementedError("CloudFormation has no Azure OpenAI deployments")

    @classmethod
    def extract_arm(cls, resource: dict) -> ResourceExtract:
        address = resource.get(ARM_ADDRESS_KEY, "")
        parameters = resource.get(ARM_PARAMETERS_KEY, {})
        # The name is `{account}/{deployment}`.
        account_ref = address.partition(":")[2].split("/")[0] or None
        model = {key: resolve_arm_value(value, parameters)[0]
                 for key, value in (_arm_properties(resource).get("model") or {}).items()}
        sku, _ = resolve_arm_value(_arm_sku_name(resource), parameters)
        return cls._extract(address, resource, account_ref, model, sku)


# Blob Storage access tiers and redundancy types (#375). A general-purpose v2
# account has rows for each pair, except those `_BLOB_UNPRICED` lists: Azure
# gives Hot and Cool RA-GZRS no write meter of their own, and Archive has no
# zone-redundant option.
_BLOB_TIERS = {"hot": "Hot", "cool": "Cool", "cold": "Cold", "archive": "Archive"}
_BLOB_REPLICATIONS = {"lrs": "LRS", "zrs": "ZRS", "grs": "GRS", "ragrs": "RA-GRS",
                      "gzrs": "GZRS", "ragzrs": "RA-GZRS"}
_BLOB_UNPRICED = {("Hot", "RA-GZRS"), ("Cool", "RA-GZRS"), ("Archive", "ZRS"),
                  ("Archive", "GZRS"), ("Archive", "RA-GZRS")}


def blob_product(config: dict) -> tuple[str, str]:
    """The access tier and redundancy of a storage account's ``config``.

    Gives Hot and LRS, the defaults of ``azurerm_storage_account``, for an
    unset setting, and the setting as given when it is unknown.
    """
    tier = _text(config.get("accessTier")) or "Hot"
    replication = _text(config.get("replicationType")) or "LRS"
    return (_BLOB_TIERS.get(tier.lower(), tier),
            _BLOB_REPLICATIONS.get(replication.lower().replace("-", "").replace("_", ""),
                                   replication))


def blob_pricing_warning(address: str, config: dict) -> Optional[str]:
    """Why the catalog has no rows for a storage account's settings, or ``None``."""
    account_tier = _text(config.get("accountTier"))
    if account_tier and account_tier.lower() != "standard":
        return (f"{address}: storage account tier {account_tier!r} has no catalog rows, "
                f"so the engine reports its usage as unpriced. The catalog prices "
                f"Standard general-purpose v2 accounts.")
    tier, replication = blob_product(config)
    if (tier not in _BLOB_TIERS.values() or replication not in _BLOB_REPLICATIONS.values()
            or (tier, replication) in _BLOB_UNPRICED):
        return (f"{address}: Blob Storage {tier} {replication} has no catalog rows, so the "
                f"engine reports its usage as unpriced.")
    return None


class AzureBlobStorage(StorageResource):
    """Azure Blob Storage - storage node (equivalent to S3).

    The access tier and the redundancy of the account select the rows
    (#375). Cool, Cold and Archive also bill data retrieval and early
    deletion, which no metric counts yet.
    """

    @property
    def valid_metrics(self) -> list[str]:
        return ["storageGb", "readRequests", "writeRequests", "dataOutGb"]

    @property
    def catalog_metrics(self) -> dict[str, str]:
        # Hot tier with LRS, the defaults of azurerm_storage_account.
        return {"storageGb": "Blob-Hot-LRS-GB-Month",
                "readRequests": "Blob-Hot-Read-Operation",
                "writeRequests": "Blob-Hot-LRS-Write-Operation",
                "dataOutGb": _EGRESS_METRIC}

    @property
    def catalog_services(self) -> dict[str, str]:
        return {_EGRESS_METRIC: _EGRESS_SERVICE}

    def catalog_metrics_for(self, config: dict) -> dict[str, str]:
        config = config or {}
        tier, replication = blob_product(config)
        account_tier = _text(config.get("accountTier")) or "Standard"
        standard = account_tier.lower() == "standard"
        if standard and (tier, replication) == ("Hot", "LRS"):
            return self.catalog_metrics
        prefix = f"Blob-{tier}-{replication.replace('-', '')}"
        if not standard:
            # A Premium account has no rows, so its usage is unpriced
            # instead of priced at Standard rates.
            prefix = f"Blob-{account_tier}-{tier}-{replication.replace('-', '')}"
        return {**self.catalog_metrics,
                "storageGb": f"{prefix}-GB-Month",
                "readRequests": f"{prefix}-Read-Operation",
                "writeRequests": f"{prefix}-Write-Operation"}

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["AzureBlobStorage"]:
        if (resource_address.startswith("azurerm_storage_account.") or
                "azure:storage:Account:" in resource_address or
                matches_arm_type(resource_address, "Microsoft.Storage/storageAccounts")):
            return cls()
        return None

    @staticmethod
    def _extract(address: str, region: Optional[str], config: dict) -> ResourceExtract:
        warning = blob_pricing_warning(address, config)
        if warning:
            warnings.warn(warning)
        return ResourceExtract(
            resource_address=address,
            node_type="storage",
            provider="azure",
            service="BlobStorage",
            region=region,
            config=config,
        )

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values") or {}
        return cls._extract(resource.get("address", ""), values.get("location"), {
            "accountTier": values.get("account_tier"),
            "replicationType": values.get("account_replication_type"),
            "accessTier": values.get("access_tier"),
        })

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs") or {}
        tier, replication = inputs.get("accountTier"), inputs.get("accountReplicationType")
        if tier is None and isinstance(inputs.get("sku"), dict):
            # azure-native joins them in the SKU name, as in `Standard_GRS`.
            tier, _, replication = (inputs["sku"].get("name") or "").partition("_")
        return cls._extract(resource.get("id", ""), inputs.get("location"), {
            "accountTier": tier or None,
            "replicationType": replication or None,
            "accessTier": inputs.get("accessTier"),
        })

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
        parameters = resource.get(ARM_PARAMETERS_KEY, {})
        sku, _ = resolve_arm_value(_arm_sku_name(resource), parameters)
        tier, _, replication = (sku if isinstance(sku, str) else "").partition("_")
        access_tier, _ = resolve_arm_value(_arm_properties(resource).get("accessTier"),
                                           parameters)
        return cls._extract(resource.get(ARM_ADDRESS_KEY, ""), arm_region(resource), {
            "accountTier": tier or None,
            "replicationType": replication or None,
            "accessTier": access_tier,
        })
