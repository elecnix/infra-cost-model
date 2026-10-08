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


# Flex Consumption instance memory sizes, in MB (#383).
_FLEX_INSTANCE_MB = (512, 2048, 4096)


def _always_ready_instances(config: Any) -> float:
    """The always-ready instances of a Flex Consumption app (#407).

    Zero when the app configures none, which bills every execution at the
    on-demand rates.
    """
    count = (config or {}).get("alwaysReadyInstances")
    return count if isinstance(count, (int, float)) else 0


def _flex_concurrency(config: Any) -> float:
    """How many executions one Flex Consumption instance runs at once (#407).

    One, which bills each execution its own duration, when the model states
    no concurrency.
    """
    factor = (config or {}).get("concurrency")
    if isinstance(factor, (int, float)) and factor >= 1:
        return factor
    return 1


def _always_ready_count(site_config: Any) -> int:
    """The always-ready instances an ARM site configures (#407).

    Azure states them as a list of ``{name, instanceCount}`` entries.
    """
    entries = (site_config or {}).get("alwaysReady") or []
    return sum(int(entry["instanceCount"]) for entry in entries
               if isinstance(entry, dict)
               and isinstance(entry.get("instanceCount"), (int, float)))


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
        if resource.get("type") not in ("azurerm_service_plan", "azurerm_app_service_plan"):
            continue
        values = resource.get("values") or {}
        sku, tier, _, _ = _tf_plan_settings(values)
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
        return ["invocations", "avgDurationMs", "memoryMb", "alwaysReadyGBHours"]

    def catalog_metrics_for(self, config: dict) -> dict:
        """The always-ready baseline of a Flex Consumption app (#407).

        An always-ready instance bills a GB-second for every second it holds
        its memory ready, idle included, so the ``alwaysReadyGBHours`` usage
        metric is the always-ready capacity the app keeps: the always-ready
        instances times the memory of one instance times the hours they stay
        ready. One GB-hour is 3,600 GB-seconds.
        """
        if (config or {}).get("hostingPlan") != "flexConsumption":
            return {}
        return {"alwaysReadyGBHours":
                {"AzureFunctionsFlex-AlwaysReady-Baseline-GB-Second": 3600}}

    def derive_catalog_usage(self, usage: dict[str, float],
                             config: Optional[dict] = None) -> Optional[DerivedCatalogUsage]:
        """Derive the executions and GB-seconds that the app's plan bills (#383).

        On the consumption plan, Azure rounds memory up to the next 128 MB
        and bills at least 100 ms for each execution. On Flex Consumption it
        bills the memory of the instance size (512 MB, 2 GB or 4 GB) at its
        own rates. The handler counts each execution's duration, at least
        100 ms, and divides the total by the ``concurrency`` setting: an
        instance bills its time once for the executions it runs together, so
        without that setting this is an upper bound (#407). On an Elastic
        Premium or dedicated plan, the plan's instances pay for every
        execution, so the app derives no quantity.
        """
        inputs = ("invocations", "avgDurationMs", "memoryMb")
        if not all(name in usage for name in inputs):
            return None
        plan = (config or {}).get("hostingPlan") or "consumption"
        if plan in ("premium", "dedicated"):
            return DerivedCatalogUsage(consumed=frozenset(inputs), quantities={})
        invocations = usage["invocations"]
        seconds = max(usage["avgDurationMs"], 100.0) / 1000
        if plan == "flexConsumption":
            memory_mb = next((mb for mb in _FLEX_INSTANCE_MB if usage["memoryMb"] <= mb),
                             _FLEX_INSTANCE_MB[-1])
            # An app with always-ready instances bills its executions on the
            # always-ready meters. Which executions land on those instances and
            # which scale out is not modelled, so a mix is priced at the
            # always-ready rates (#407).
            ready = "AlwaysReady-" if _always_ready_instances(config) else ""
            return DerivedCatalogUsage(
                consumed=frozenset(inputs),
                quantities={
                    f"AzureFunctionsFlex-{ready}Execution": invocations,
                    f"AzureFunctionsFlex-{ready}GB-Second":
                        invocations * memory_mb / 1024 * seconds / _flex_concurrency(config),
                },
            )
        memory_gb = math.ceil(usage["memoryMb"] / 128) * 128 / 1024
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

        Each plan gets its own service. `derive_catalog_usage` prices the
        app by its plan, and the plan's own node prices its instances
        (#383). An app whose plan isn't in the input is priced as a
        consumption plan app, with a UserWarning.
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
        service, _ = _HOSTING_PLANS[kind]
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
        always_ready = _always_ready_count(properties.get("siteConfig"))
        config = {
            "sku": properties.get("serverFarmId"),
            "runtime": app_settings.get("FUNCTIONS_WORKER_RUNTIME"),
            **plan,
        }
        if always_ready:
            config["alwaysReadyInstances"] = always_ready
        return ResourceExtract(
            resource_address=address,
            node_type="compute",
            provider="azure",
            service=service,
            region=arm_region(resource),
            config=config,
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


# Key that the `extract_resources_from_*` functions add to each resource: the
# Cosmos DB databases and containers of the input, whose throughput the
# account bills (#399).
COSMOS_THROUGHPUT_KEY = "_cosmosThroughput"


@dataclass(frozen=True)
class CosmosThroughput:
    """A Cosmos DB database or container of the input, and its throughput.

    ``account`` names the account it belongs to and ``database`` the database
    a container belongs to, which is ``None`` for a database. ``ru_per_second``
    is a manual throughput and ``autoscale_max_ru`` the maximum of autoscale
    settings; a serverless database or container sets neither.
    """

    account: Optional[str]
    name: Optional[str]
    database: Optional[str]
    ru_per_second: Optional[float]
    autoscale_max_ru: Optional[float]

    def bills_throughput(self) -> bool:
        return self.ru_per_second is not None or self.autoscale_max_ru is not None


def _cosmos_ru(value: Any) -> Optional[float]:
    """A throughput setting in RU/s, or ``None`` when the resource sets none."""
    return value if isinstance(value, (int, float)) else None


def _cosmos_entry(account: Any, name: Any, database: Any, throughput: Any,
                  max_ru: Any) -> CosmosThroughput:
    return CosmosThroughput(account=_text(account), name=_text(name),
                            database=_text(database), ru_per_second=_cosmos_ru(throughput),
                            autoscale_max_ru=_cosmos_ru(max_ru))


def cosmos_units(config: dict) -> Optional[float]:
    """The account's throughput in 100 RU/s units, the Azure billing unit.

    Autoscale bills the highest RU/s of the hour, so its maximum is the
    throughput of the account (#399). Gives ``None`` when the ``config``
    gives no throughput, so the node counts the hours of 100 RU/s itself.
    An autoscale maximum of 0 is a setting the input declares, so it wins
    over a manual throughput; only an absent one falls back. An account
    that sets both prices each on its own meter in
    ``CosmosDB.catalog_metrics_for``.
    """
    ru = config.get("autoscaleMaxRuPerSecond")
    if ru is None:
        ru = config.get("throughputRuPerSecond")
    return float(ru) / 100 if isinstance(ru, (int, float)) else None


def cosmos_throughput(entries: list, account: Any) -> dict:
    """The ``config`` throughput of a Cosmos DB account (#399).

    ``entries`` are the databases and containers of the input and
    ``account`` the account's name. Azure bills the throughput of every
    database and container of an account, and a database shares its
    throughput with its containers, so a container of a database that sets
    a throughput is left out. Autoscale bills the highest RU/s of the
    hour, so it contributes ``autoscaleMaxRuPerSecond``, which the
    autoscale rows price beside the manual ``throughputRuPerSecond``.

    Gives ``None`` for both keys when nothing sets a throughput, as on a
    serverless account and on a database that shares another account's.
    """
    name = account.lower() if isinstance(account, str) else ""
    mine = [e for e in entries if name and e.account and e.account.lower() == name]
    shared = {(e.name or "").lower() for e in mine
              if e.database is None and e.bills_throughput()}
    billed = [e for e in mine
              if e.database is None or (e.database or "").lower() not in shared]
    autoscale = sum(e.autoscale_max_ru or 0.0 for e in billed)
    manual = sum(e.ru_per_second or 0.0 for e in billed)
    return {"throughputRuPerSecond": manual or None,
            "autoscaleMaxRuPerSecond": autoscale or None}


def cosmos_throughput_from_tf(resources: list) -> list:
    """The ``azurerm_cosmosdb_sql_database`` and ``azurerm_cosmosdb_sql_container``."""
    entries = []
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        values = resource.get("values") or {}
        if resource.get("type") == "azurerm_cosmosdb_sql_database":
            database, name = None, values.get("name")
        elif resource.get("type") == "azurerm_cosmosdb_sql_container":
            database, name = values.get("database"), values.get("name")
        else:
            continue
        autoscale = _first_block(values.get("autoscale_settings"))
        entries.append(_cosmos_entry(values.get("account"), name, database,
                                     values.get("throughput"),
                                     autoscale.get("max_throughput")))
    return entries


# Each Pulumi Cosmos DB SQL resource: the input that names the account, the
# database a container belongs to (a database has none), and its own name.
_PULUMI_COSMOS_SQL = {
    "azure-native:documentdb:DatabaseAccountSqlDatabase": ("accountName", None, "databaseName"),
    "azure-native:documentdb:DatabaseAccountSqlContainer": ("accountName", "databaseName",
                                                            "containerName"),
    "azure:cosmosdb:SqlDatabase": ("account", None, "name"),
    "azure:cosmosdb:SqlContainer": ("account", "database_name", "container_name"),
}

_ARM_COSMOS_SQL_DATABASE = "microsoft.documentdb/databaseaccounts/sqldatabases"
_ARM_COSMOS_SQL_CONTAINER = _ARM_COSMOS_SQL_DATABASE + "/sqlcontainers"


def cosmos_throughput_from_pulumi(resources: list) -> list:
    """The Cosmos DB databases and containers of a Pulumi stack export."""
    entries = []
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        resource_type = resource.get("type", "")
        names = _PULUMI_COSMOS_SQL.get(resource_type)
        if names is None:
            continue
        inputs = resource.get("inputs") or {}
        account, database, name = (inputs.get(key) if key else None for key in names)
        if resource_type.startswith("azure-native:"):
            options = inputs.get("options") or {}
            autoscale = options.get("autoscaleSettings") or {}
            throughput, max_ru = options.get("throughput"), autoscale.get("maxThroughput")
        else:
            autoscale = inputs.get("autoscale_settings") or {}
            throughput, max_ru = inputs.get("throughput"), autoscale.get("max_throughput")
        entries.append(_cosmos_entry(account, name, database, throughput, max_ru))
    return entries


def cosmos_throughput_from_arm(resources) -> list:
    """The Cosmos DB databases and containers of ``(address, resource)`` pairs.

    An ARM child names its account first: ``{account}/{database}`` for a
    database, ``{account}/{database}/{container}`` for a container. A name
    that a template builds cannot name the account, so the entry has none.
    """
    entries = []
    for address, resource in resources:
        arm_type, _, name = address.partition(":")
        arm_type = arm_type.lower()
        if arm_type not in (_ARM_COSMOS_SQL_DATABASE, _ARM_COSMOS_SQL_CONTAINER):
            continue
        container = arm_type == _ARM_COSMOS_SQL_CONTAINER
        segments = name.split("/")
        if len(segments) < (3 if container else 2):
            account, database, name = None, None, None
        elif container:
            account, database, name = segments[0], segments[1], segments[2]
        else:
            account, database, name = segments[0], None, segments[1]
        parameters = resource.get(ARM_PARAMETERS_KEY, {})
        properties = _arm_properties(resource)
        options = properties.get("options") or {}
        autoscale = options.get("autoscaleSettings") or {}
        # A template written before `options` existed states the throughput
        # beside the other resource settings.
        throughput = resolve_arm_value(
            options.get("throughput", properties.get("throughput")), parameters)[0]
        entries.append(_cosmos_entry(
            account, name, database, throughput,
            resolve_arm_value(autoscale.get("maxThroughput"), parameters)[0]))
    return entries


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

    The throughput lives on the account's databases and containers, which the
    extractors read into the node's ``config`` (#399). ``throughputHours``
    then counts the hours of a month the account bills, and the handler
    prices each of them at the account's throughput: an autoscale account
    prices ``CosmosDB-Autoscale-100RU-Hour``, which costs 1.5 times the
    manual rate, at its maximum RU/s. A node whose ``config`` gives no
    throughput counts the hours of 100 RU/s itself.
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
        autoscale = config.get("autoscaleMaxRuPerSecond")
        manual = config.get("throughputRuPerSecond")
        manual_row = ("CosmosDB-Provisioned-MultiRegionWrite-100RU-Hour"
                      if config.get("multiRegionWrites")
                      else "CosmosDB-Provisioned-100RU-Hour")
        # Autoscale bills 1.5 times the manual rate, at its maximum RU/s.
        autoscale_row = "CosmosDB-Autoscale-100RU-Hour"
        if isinstance(autoscale, (int, float)) and isinstance(manual, (int, float)):
            # An account whose databases mix autoscale and manual throughput
            # bills each on its own meter, so both are priced.
            return {"storageGb": "CosmosDB-Storage-GB-Month",
                    "throughputHours": {autoscale_row: autoscale / 100,
                                        manual_row: manual / 100}}
        throughput = autoscale_row if autoscale is not None else manual_row
        units = cosmos_units(config)
        return {"storageGb": "CosmosDB-Storage-GB-Month",
                "throughputHours": {throughput: units} if units else throughput}

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
    def _throughput(cls, address: str, resource: dict, account: Any) -> dict:
        """The account's ``config`` throughput, from the input's children (#399)."""
        entries = resource.get(COSMOS_THROUGHPUT_KEY, [])
        unnamed = [e for e in entries if e.account is None and e.bills_throughput()]
        if unnamed:
            warnings.warn(
                f"{address}: {len(unnamed)} Cosmos DB database or container resource(s) "
                f"set a throughput but name no account, so this account is priced "
                f"without it. Give each of them the account they belong to."
            )
        return cosmos_throughput(entries, account)

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        address = resource.get("address", "")
        # azurerm 4.x names it `multiple_write_locations_enabled`, 3.x
        # `enable_multiple_write_locations`.
        multi_region = values.get("multiple_write_locations_enabled",
                                  values.get("enable_multiple_write_locations"))
        return ResourceExtract(
            resource_address=address,
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
                **cls._throughput(address, resource, values.get("name")),
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        address = resource.get("id", "")
        properties = inputs.get("properties") or {}
        account = (inputs.get("accountName") or inputs.get("name")
                   or _last_segment(address))
        return ResourceExtract(
            resource_address=address,
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
                **cls._throughput(address, resource, account),
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
        address = resource.get(ARM_ADDRESS_KEY, "")
        return ResourceExtract(
            resource_address=address,
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
                **cls._throughput(address, resource, address.partition(":")[2]),
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


# Blob Storage access tiers, redundancy types and account kinds (#375, #398).
# The kind selects the product the account bills: a general-purpose v2 account
# the `General Block Blob v2` one, a general-purpose v1 account (the original
# `Storage` kind) the `Blob Storage` one, and a Premium account the
# `Premium Block Blob` one. The value is the infix its metrics carry.
_BLOB_TIERS = {"hot": "Hot", "cool": "Cool", "cold": "Cold", "archive": "Archive"}
_BLOB_REPLICATIONS = {"lrs": "LRS", "zrs": "ZRS", "grs": "GRS", "ragrs": "RA-GRS",
                      "gzrs": "GZRS", "ragzrs": "RA-GZRS"}
_BLOB_KINDS = {"storagev2": "", "storage": "Storage-"}
# Archive has no zone-redundant option.
_BLOB_UNPRICED = {("Archive", "ZRS"), ("Archive", "GZRS"), ("Archive", "RA-GZRS")}
# Azure publishes no write meter for Hot and Cool RA-GZRS. It bills Hot RA-GRS
# writes on the meter `Hot GRS Write Operations` that Hot GRS writes use, and
# gives RA-GZRS no such meter: neither SKU has one in the Azure Retail Prices
# API (checked 2026-10-02, product `General Block Blob v2`, skuName
# `Hot RA-GZRS` and `Cool RA-GZRS`). Those writes are not billed separately,
# so the handler does not price them and does not warn that it cannot.
_BLOB_NO_WRITE_METER = {("Hot", "RA-GZRS"), ("Cool", "RA-GZRS")}
# The tiers that bill data retrieval, with the days a blob stays in one
# before deleting it costs: a Cool blob deleted under 30 days, a Cold one
# under 90 and an Archive one under 180
# (https://azure.microsoft.com/en-us/pricing/details/storage/blobs/). Azure
# publishes one early-deletion meter per SKU, at the tier's storage price
# for the whole window, and no meter per number of days, so `earlyDeleteGb`
# counts the GB deleted before the window and no setting selects another
# row.
_BLOB_COOL_TIERS = {"Cool": 30, "Cold": 90, "Archive": 180}
# The access tier and redundancy that publish an early-deletion meter. Azure
# gives each one a meter at the tier's storage price for the whole window,
# and none to the two zone-redundant Cool SKUs. The general-purpose v1
# product has no Cool meter either.
_BLOB_EARLY_DELETE = {
    ("Cool", "LRS"), ("Cool", "ZRS"), ("Cool", "GRS"), ("Cool", "RA-GRS"),
    ("Cold", "LRS"), ("Cold", "ZRS"), ("Cold", "GRS"), ("Cold", "RA-GRS"),
    ("Cold", "GZRS"), ("Cold", "RA-GZRS"),
    ("Archive", "LRS"), ("Archive", "GRS"), ("Archive", "RA-GRS"),
}
_BLOB_V1_EARLY_DELETE_TIERS = {"Cold", "Archive"}


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


def blob_account_kind(config: dict) -> Optional[str]:
    """The kind of a storage account, or ``None`` for a kind with no rows.

    Gives `storagev2`, the default of ``azurerm_storage_account``, for an
    unset kind.
    """
    kind = (_text(config.get("accountKind")) or "StorageV2").lower()
    kind = kind.replace("-", "").replace("_", "")
    return kind if kind in _BLOB_KINDS else None


def blob_metric_prefix(config: dict) -> Optional[str]:
    """The prefix of the catalog metrics an account's settings select.

    ``None`` when the Azure Retail Prices API has no product for them, so
    the account's usage is reported unpriced rather than priced at another
    product's rates.
    """
    account_tier = (_text(config.get("accountTier")) or "Standard").lower()
    tier, replication = blob_product(config)
    if account_tier == "premium":
        # A Premium block blob account has no access tier of its own: its SKU
        # names the redundancy alone.
        return f"Blob-Premium-{replication}" if replication in ("LRS", "ZRS") else None
    kind = blob_account_kind(config) if account_tier == "standard" else None
    if kind is None or (tier, replication) in _BLOB_UNPRICED:
        return None
    return f"Blob-{_BLOB_KINDS[kind]}{tier}-{replication.replace('-', '')}"


def blob_pricing_warning(address: str, config: dict) -> Optional[str]:
    """Why the catalog has no rows for a storage account's settings, or ``None``."""
    if blob_metric_prefix(config) is None:
        tier, replication = blob_product(config)
        settings = " ".join(part for part in (
            _text(config.get("accountTier")), _text(config.get("accountKind")),
            tier, replication) if part)
        return (f"{address}: Blob Storage {settings} has no catalog rows, so the "
                f"engine reports its usage as unpriced.")
    return None


class AzureBlobStorage(StorageResource):
    """Azure Blob Storage - storage node (equivalent to S3).

    The account kind, the access tier and the redundancy select the rows
    (#375, #398). Cool, Cold and Archive also bill ``dataRetrievalGb`` per
    GB read back, and ``earlyDeleteGb`` for the blobs deleted before the
    tier's minimum retention.
    """

    @property
    def valid_metrics(self) -> list[str]:
        return ["storageGb", "readRequests", "writeRequests", "dataOutGb",
                "dataRetrievalGb", "earlyDeleteGb"]

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
        account_tier = (_text(config.get("accountTier")) or "Standard").lower()
        if (tier, replication) == ("Hot", "LRS") and account_tier == "standard" \
                and blob_account_kind(config) == "storagev2":
            return self.catalog_metrics
        prefix = blob_metric_prefix(config)
        unpriced = prefix is None
        if unpriced:
            # No product for these settings: name the metrics after them, so
            # the catalog cannot resolve them and the engine reports the
            # account's usage as unpriced.
            prefix = "Blob-Unpriced-{}".format("-".join(
                part for part in (_text(config.get("accountTier")),
                                  _text(config.get("accountKind")), tier, replication)
                if part))
        metrics = {"storageGb": f"{prefix}-GB-Month",
                   "readRequests": f"{prefix}-Read-Operation",
                   "dataOutGb": _EGRESS_METRIC}
        if (tier, replication) not in _BLOB_NO_WRITE_METER:
            metrics["writeRequests"] = f"{prefix}-Write-Operation"
        if not unpriced and tier in _BLOB_COOL_TIERS:
            metrics["dataRetrievalGb"] = f"{prefix}-Retrieval-GB"
            early_delete = _BLOB_EARLY_DELETE
            if blob_account_kind(config) == "storage":
                early_delete = {(t, r) for t, r in early_delete
                                if t in _BLOB_V1_EARLY_DELETE_TIERS}
            if (tier, replication) in early_delete:
                metrics["earlyDeleteGb"] = f"{prefix}-Early-Delete-GB"
        return metrics

    def derive_catalog_usage(self, usage: dict[str, float],
                             config: Optional[dict] = None) -> Optional[DerivedCatalogUsage]:
        """The writes that the account's settings do not bill separately.

        Azure publishes no write meter for Hot and Cool RA-GZRS, so those
        writes are not charged and no catalog quantity names them.
        """
        if "writeRequests" not in usage:
            return None
        tier, replication = blob_product(config or {})
        if (tier, replication) not in _BLOB_NO_WRITE_METER:
            return None
        return DerivedCatalogUsage(consumed=frozenset({"writeRequests"}), quantities={})

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
            "accountKind": values.get("account_kind"),
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
            "accountKind": inputs.get("kind"),
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
                "accountKind": properties.get("Kind"),
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
            "accountKind": _text(resource.get("kind")),
            "replicationType": replication or None,
            "accessTier": access_tier,
        })


# Dedicated App Service plan SKUs with instance-hour rows (#383, #407), by the
# name that the metric uses.
_DEDICATED_SKUS = {sku.lower(): sku for sku in (
    "B1", "B2", "B3", "S1", "S2", "S3", "P1v2", "P2v2", "P3v2", "P0v3", "P1v3", "P2v3",
    "P3v3", "P1mv3", "P2mv3", "P3mv3", "P4mv3", "P5mv3", "P1", "P2", "P3", "P4", "PC2",
    "PC3", "PC4", "P0v4", "P1v4", "P2v4", "P3v4", "P1mv4", "P2mv4", "P3mv4", "P4mv4",
    "P5mv4", "I1", "I2", "I3", "I12", "I13", "I14", "I1v2", "I2v2", "I3v2", "I4v2",
    "I5v2", "I6v2", "I1mv2", "I2mv2", "I3mv2", "I4mv2", "I5mv2", "I1v4", "I2v4", "I3v4",
    "I4v4", "I5v4", "I6v4", "I1mv4", "I2mv4", "I3mv4", "I4mv4", "I5mv4")}
# The dedicated SKUs Azure sells on one operating system only: the classic
# Premium plans are Windows-only and the Premium container plans (Xenon) run
# Windows containers (#407).
_WINDOWS_ONLY_SKUS = frozenset({"p1", "p2", "p3", "p4"})
_WINDOWS_CONTAINER_SKUS = frozenset({"pc2", "pc3", "pc4"})
# Elastic Premium SKUs: the vCPUs and GiB of memory of each instance, which
# Azure bills per hour.
_ELASTIC_PREMIUM = {"ep1": (1, 3.5), "ep2": (2, 7), "ep3": (4, 14)}


def _plan_sku(sku: Any) -> Optional[str]:
    """A plan SKU as the metrics name it: `P1 v3` and `p1v3` are `P1v3`."""
    text = _text(sku)
    if text is None:
        return None
    compact = text.replace(" ", "")
    return _DEDICATED_SKUS.get(compact.lower(), compact)


def _dedicated_sku_priced(sku: Any, os_name: str) -> bool:
    """Whether the metric of the dedicated plan ``sku`` on ``os_name`` has rows (#407).

    Each SKU family has rows for the operating systems Azure sells it on: Linux
    and Windows for most, Windows alone for the classic Premium plans, and
    Windows containers for the Premium container (Xenon) plans.
    """
    name = (sku or "").lower()
    if name in _WINDOWS_CONTAINER_SKUS:
        return os_name == "WindowsContainer"
    if name in _WINDOWS_ONLY_SKUS:
        return os_name == "Windows"
    return name in _DEDICATED_SKUS and os_name in ("Linux", "Windows")


def _plan_os(value: Any, reserved: Any = None) -> str:
    """``Linux``, ``Windows`` or ``WindowsContainer`` from an OS type or a kind."""
    text = (_text(value) or "").lower()
    if text == "windowscontainer" or "xenon" in text:
        return "WindowsContainer"
    if "linux" in text or reserved is True:
        return "Linux"
    return "Windows"


def _tf_plan_settings(values: dict) -> tuple:
    """A Terraform plan's SKU, tier, OS and instance count (#407).

    ``azurerm_service_plan`` states ``sku_name``, ``os_type`` and
    ``worker_count``; ``azurerm_app_service_plan`` states a ``sku`` block of
    ``tier``, ``size`` and ``capacity``, and names its OS in ``kind`` with
    ``reserved``. A plan carries one of the two shapes, so read whichever it
    has: branching on the resource type is how the two schemas got swapped.
    """
    block = _first_block(values.get("sku"))
    return (block.get("size") or values.get("sku_name"),
            block.get("tier"),
            _plan_os(values.get("os_type") or values.get("kind"), values.get("reserved")),
            block.get("capacity") or values.get("worker_count"))


class AppServicePlan(ComputeResource):
    """An App Service plan, which bills its instances (#383).

    A dedicated plan bills each instance-hour of its SKU. An Elastic
    Premium plan bills the vCPU-hours and GiB-hours of its instances,
    always-ready ones included. The ``instanceHours`` metric counts the
    instance-hours of a month, and is usually fixed. The Function Apps on
    these plans cost nothing per execution. A consumption or Flex
    Consumption plan has no cost of its own: its apps pay per execution.
    """

    @property
    def valid_metrics(self) -> list[str]:
        return ["instanceHours"]

    @property
    def catalog_metrics(self) -> dict[str, str]:
        return {}

    def catalog_metrics_for(self, config: dict) -> dict:
        config = config or {}
        plan = config.get("hostingPlan")
        sku = _plan_sku(config.get("sku"))
        if plan == "premium":
            vcpus, memory = _ELASTIC_PREMIUM.get((sku or "").lower(), (None, None))
            if vcpus is None:
                return {"instanceHours": f"AzureFunctionsPremium-{sku}-Instance-Hour"}
            return {"instanceHours": {"AzureFunctionsPremium-vCPU-Hour": vcpus,
                                      "AzureFunctionsPremium-GiB-Hour": memory}}
        if plan == "dedicated" and sku:
            os_name = _plan_os(config.get("os"))
            return {"instanceHours": f"AppService-{os_name}-{sku}-Instance-Hour"}
        return {}

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["AppServicePlan"]:
        if (resource_address.startswith(("azurerm_service_plan.", "azurerm_app_service_plan.")) or
                matches_arm_type(resource_address, "Microsoft.Web/serverfarms")):
            return cls()
        return None

    @staticmethod
    def _extract(address: str, region: Any, sku: Any, tier: Any, os_name: str,
                 instances: Any) -> ResourceExtract:
        plan = hosting_plan(sku, tier)
        service = _HOSTING_PLANS[plan][0] if plan else "AppService"
        config = {"sku": _text(sku), "tier": _text(tier), "hostingPlan": plan,
                  "os": os_name, "instances": instances}
        name = _plan_sku(sku)
        known = ((plan == "dedicated" and _dedicated_sku_priced(name, os_name))
                 or (plan == "premium" and (name or "").lower() in _ELASTIC_PREMIUM)
                 or plan in ("consumption", "flexConsumption"))
        if not known:
            warnings.warn(
                f"{address}: App Service plan SKU {sku or tier!r} on {os_name} has no "
                f"catalog rows, so the engine reports its instance-hours as unpriced."
            )
        return ResourceExtract(resource_address=address, node_type="compute",
                               provider="azure", service=service, region=region,
                               config=config)

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values") or {}
        sku, tier, os_name, instances = _tf_plan_settings(values)
        return cls._extract(resource.get("address", ""), values.get("location"),
                            sku, tier, os_name, instances)

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs") or {}
        resource_type = resource.get("type", "")
        if resource_type == "azure:appservice/servicePlan:ServicePlan":
            return cls._extract(resource.get("id", ""), inputs.get("location"),
                                inputs.get("skuName"), None, _plan_os(inputs.get("osType")),
                                inputs.get("workerCount"))
        sku = inputs.get("sku") or {}
        return cls._extract(resource.get("id", ""), inputs.get("location"),
                            sku.get("name", sku.get("size")), sku.get("tier"),
                            _plan_os(inputs.get("kind"), inputs.get("reserved")),
                            sku.get("capacity"))

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        raise NotImplementedError("CloudFormation has no App Service plans")

    @classmethod
    def extract_arm(cls, resource: dict) -> ResourceExtract:
        parameters = resource.get(ARM_PARAMETERS_KEY, {})
        sku = resource.get("sku") or {}
        name, _ = resolve_arm_value(sku.get("name"), parameters)
        tier, _ = resolve_arm_value(sku.get("tier"), parameters)
        capacity, _ = resolve_arm_value(sku.get("capacity"), parameters)
        kind, _ = resolve_arm_value(resource.get("kind"), parameters)
        return cls._extract(resource.get(ARM_ADDRESS_KEY, ""), arm_region(resource), name, tier,
                            _plan_os(kind, _arm_properties(resource).get("reserved")), capacity)
