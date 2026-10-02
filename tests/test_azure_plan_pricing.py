"""App Service plans and the Function Apps on them are priced (#383).

A plan node bills its instance-hours: the SKU's hourly rate on a dedicated
plan, and vCPU-hours plus GiB-hours on an Elastic Premium plan. A Function
App on either plan costs nothing per execution, so the plan's cost isn't
counted twice. A Flex Consumption app bills executions and GB-seconds at
Flex Consumption rates.
"""
import json
import warnings

import pytest

from infra_cost_model.engine.engine import CostEngine
from infra_cost_model.pricing.cache import SEED_PRICES_PATH
from infra_cost_model.pricing.free_tiers import ACCOUNT, free_tier_scope
from infra_cost_model.pricing.sources import infracost as ic
from infra_cost_model.resources.azure import AppServicePlan, AzureFunction
from infra_cost_model.resources.registry import (
    ResourceRegistry, extract_resources_from_arm, extract_resources_from_pulumi,
    extract_resources_from_tf,
)

REGION = "eastus"
RG = "/subscriptions/0000/resourceGroups/rg/providers"


def seeded(service):
    rows = json.loads(SEED_PRICES_PATH.read_text())
    return {r["usage_metric"] for r in rows if r["service"] == service and r["region"] == REGION}


def compute(nodes, catalog, entry, per_month=1):
    model = {"version": "1.0",
             "workflow": {"name": "w", "entry": entry,
                          "frequency": {"unit": "perMonth", "value": per_month}},
             "nodes": nodes, "edges": []}
    engine = CostEngine(model, catalog=catalog, time_basis="monthly")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        costs = engine.compute()
    return costs, engine


def plan_node(address, service, config, hours):
    return {"nodeType": "compute", "resourceAddress": address, "provider": "azure",
            "service": service, "region": REGION, "config": config,
            "usageMetrics": {"instanceHours": {"unit": "hours", "value": hours, "fixed": True}}}


# --- Plan handler ------------------------------------------------------------------


@pytest.mark.parametrize("address", [
    "azurerm_service_plan.plan", "azurerm_app_service_plan.plan",
    "Microsoft.Web/serverfarms:plan", f"{RG}/Microsoft.Web/serverfarms/plan",
])
def test_plan_addresses_have_a_handler(address):
    assert ResourceRegistry.from_address(address) is AppServicePlan


@pytest.mark.parametrize("sku,os,metric", [
    ("P1v3", "Linux", "AppService-Linux-P1v3-Instance-Hour"),
    ("P1 v3", "Windows", "AppService-Windows-P1v3-Instance-Hour"),
    ("B1", "linux", "AppService-Linux-B1-Instance-Hour"),
    ("S2", None, "AppService-Windows-S2-Instance-Hour"),
    ("P2mv3", "Linux", "AppService-Linux-P2mv3-Instance-Hour"),
])
def test_dedicated_plan_bills_its_sku_per_instance_hour(sku, os, metric):
    config = {"sku": sku, "os": os, "hostingPlan": "dedicated"}
    assert AppServicePlan().catalog_metrics_for(config) == {"instanceHours": metric}
    assert metric in seeded("AppService")


@pytest.mark.parametrize("sku,vcpus,memory", [("EP1", 1, 3.5), ("EP2", 2, 7), ("EP3", 4, 14)])
def test_elastic_premium_bills_vcpu_and_memory_hours(sku, vcpus, memory):
    config = {"sku": sku, "hostingPlan": "premium"}
    assert AppServicePlan().catalog_metrics_for(config) == {"instanceHours": {
        "AzureFunctionsPremium-vCPU-Hour": vcpus,
        "AzureFunctionsPremium-GiB-Hour": memory}}


def test_consumption_and_flex_plans_have_no_plan_cost():
    for plan in ("consumption", "flexConsumption"):
        assert AppServicePlan().catalog_metrics_for({"hostingPlan": plan}) == {}


def test_dedicated_plan_is_priced(seed_catalog):
    # 2 P1v3 Linux instances for a 730-hour month at $0.155 an hour.
    address = "azurerm_service_plan.plan"
    node = plan_node(address, "AppService",
                     {"sku": "P1v3", "os": "Linux", "hostingPlan": "dedicated"}, 2 * 730)
    costs, engine = compute({address: node}, seed_catalog, address)
    assert costs[address] == pytest.approx(2 * 730 * 0.155)
    assert engine.unpriced_metrics == []


def test_elastic_premium_plan_is_priced(seed_catalog):
    # One EP2 instance for 730 hours: 2 vCPU at $0.173 and 7 GiB at $0.0123.
    address = "azurerm_service_plan.plan"
    node = plan_node(address, "AzureFunctionsPremium",
                     {"sku": "EP2", "hostingPlan": "premium"}, 730)
    costs, engine = compute({address: node}, seed_catalog, address)
    assert costs[address] == pytest.approx(730 * (2 * 0.173 + 7 * 0.0123))
    assert engine.unpriced_metrics == []


def test_unknown_sku_warns():
    resource = {"address": "azurerm_service_plan.plan", "type": "azurerm_service_plan",
                "values": {"location": REGION, "sku_name": "I1v2", "os_type": "Linux"}}
    with pytest.warns(UserWarning, match=r"azurerm_service_plan.plan.*I1v2"):
        extract_resources_from_tf({"resource": [resource]})


def test_every_plan_metric_has_a_descriptor():
    for service in ("AppService", "AzureFunctionsPremium", "AzureFunctionsFlexConsumption"):
        for metric in seeded(service):
            assert ic.METRIC_DESCRIPTORS[metric]["store_service"] == service


# --- Plan extraction ------------------------------------------------------------------


def test_terraform_service_plan():
    resource = {"address": "azurerm_service_plan.plan", "type": "azurerm_service_plan",
                "values": {"location": REGION, "sku_name": "P1v3", "os_type": "Linux",
                           "worker_count": 3}}
    node = extract_resources_from_tf({"resource": [resource]})["azurerm_service_plan.plan"]
    assert node["service"] == "AppService"
    assert node["region"] == REGION
    assert node["config"] == {"sku": "P1v3", "tier": None, "hostingPlan": "dedicated",
                              "os": "Linux", "instances": 3}


def test_arm_premium_plan():
    template = {"resources": [{
        "type": "Microsoft.Web/serverfarms", "name": "plan", "location": REGION, "kind": "elastic",
        "sku": {"name": "EP1", "tier": "ElasticPremium", "capacity": 1},
        "properties": {"reserved": True}}]}
    node = extract_resources_from_arm(template)["Microsoft.Web/serverfarms:plan"]
    assert node["service"] == "AzureFunctionsPremium"
    assert node["config"]["hostingPlan"] == "premium"
    assert node["config"]["os"] == "Linux"
    assert node["config"]["instances"] == 1


def test_pulumi_azure_native_plan():
    stack = {"deployment": {"resources": [{
        "id": f"{RG}/Microsoft.Web/serverfarms/plan", "type": "azure-native:web:AppServicePlan",
        "inputs": {"location": REGION, "kind": "linux", "reserved": True,
                   "sku": {"name": "S1", "tier": "Standard", "capacity": 2}}}]}}
    node = extract_resources_from_pulumi(stack)[f"{RG}/Microsoft.Web/serverfarms/plan"]
    assert node["config"]["sku"] == "S1"
    assert node["config"]["os"] == "Linux"
    assert node["config"]["instances"] == 2


# --- Function Apps on each plan -----------------------------------------------------------


def app_node(config):
    address = "azurerm_linux_function_app.orders"
    service = {"consumption": "AzureFunctions", "premium": "AzureFunctionsPremium",
               "flexConsumption": "AzureFunctionsFlexConsumption",
               "dedicated": "AppService"}[config["hostingPlan"]]
    return address, {
        "nodeType": "compute", "resourceAddress": address, "provider": "azure",
        "service": service, "region": REGION, "config": config,
        "usageMetrics": {"invocations": {"unit": "requests", "value": 1},
                         "avgDurationMs": {"unit": "ms", "value": 1000},
                         "memoryMb": {"unit": "MB", "value": 512}}}


@pytest.mark.parametrize("plan", ["premium", "dedicated"])
def test_app_on_a_plan_with_instances_costs_nothing_per_execution(plan, seed_catalog):
    address, node = app_node({"hostingPlan": plan})
    costs, engine = compute({address: node}, seed_catalog, address, per_month=3_000_000)
    assert costs[address] == 0
    assert engine.unpriced_metrics == []


def test_flex_consumption_app_bills_executions_and_gb_seconds(seed_catalog):
    """3M executions of 1 s on 512 MB instances: 1.5M GB-seconds.

    (3M - 250K) x $0.40/M = $1.10, and (1.5M - 100K) x $0.000026 = $36.40.
    """
    address, node = app_node({"hostingPlan": "flexConsumption"})
    costs, engine = compute({address: node}, seed_catalog, address, per_month=3_000_000)
    assert costs[address] == pytest.approx(1.10 + 36.40)
    assert engine.unpriced_metrics == []


@pytest.mark.parametrize("memory_mb,instance_gb", [(128, 0.5), (512, 0.5), (1000, 2), (2048, 2),
                                                   (3000, 4), (4096, 4)])
def test_flex_consumption_bills_the_instance_memory(memory_mb, instance_gb):
    derived = AzureFunction().derive_catalog_usage(
        {"invocations": 1.0, "avgDurationMs": 1000.0, "memoryMb": float(memory_mb)},
        {"hostingPlan": "flexConsumption"})
    assert derived.quantities == {"AzureFunctionsFlex-Execution": 1.0,
                                  "AzureFunctionsFlex-GB-Second": pytest.approx(instance_gb)}


def test_flex_free_grant_covers_the_subscription():
    for metric in ("AzureFunctionsFlex-Execution", "AzureFunctionsFlex-GB-Second"):
        assert free_tier_scope("azure", "AzureFunctionsFlexConsumption", metric) == ACCOUNT


def test_apps_on_premium_and_dedicated_plans_no_longer_warn():
    template = {"resources": [
        {"type": "Microsoft.Web/serverfarms", "name": "plan", "location": REGION,
         "sku": {"name": "P1v3", "tier": "PremiumV3"}},
        {"type": "Microsoft.Web/sites", "name": "func", "kind": "functionapp",
         "location": REGION,
         "properties": {"serverFarmId": "[resourceId('Microsoft.Web/serverfarms', 'plan')]"}}]}
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        nodes = extract_resources_from_arm(template)
    assert nodes["Microsoft.Web/sites:func"]["service"] == "AppService"
    assert nodes["Microsoft.Web/serverfarms:plan"]["service"] == "AppService"


def _sync(metric, products):
    from unittest.mock import MagicMock, patch

    def post(url, headers=None, json=None, timeout=None):
        assert json["variables"]["service"] == ic.METRIC_DESCRIPTORS[metric]["service"]
        filters = json["variables"]["attributeFilters"]
        matched = [p for p in products
                   if all({a["key"]: a["value"] for a in p["attributes"]}.get(f["key"])
                          == f["value"] for f in filters)]
        response = MagicMock(status_code=200)
        response.json.return_value = {"data": {"products": matched}}
        return response

    stored = []
    cache = MagicMock()
    cache.upsert.side_effect = stored.append
    client = ic.InfracostClient(api_key="test-token", org_id="org-123")
    with patch.object(ic.requests, "post", side_effect=post):
        client.sync_to_cache(cache, metric, REGION)
    return stored


def _product(product, sku, meter, prices):
    return {"productFamily": "Compute",
            "attributes": [{"key": "productName", "value": product},
                           {"key": "skuName", "value": sku},
                           {"key": "meterName", "value": meter}],
            "prices": [{"USD": usd, "unit": unit, "startUsageAmount": start,
                        "endUsageAmount": None} for usd, unit, start in prices]}


def test_flex_executions_sync_per_execution():
    products = [
        _product("Flex Consumption", "On Demand", "On Demand Total Executions",
                 [("0", "10", "0"), ("0.000004", "10", "25000")]),
        _product("Flex Consumption", "Always Ready", "Always Ready Total Executions",
                 [("0.000004", "10", "0")]),
    ]
    rows = _sync("AzureFunctionsFlex-Execution", products)
    assert [(r.price_usd, r.start_usage_amount, r.unit, r.service) for r in rows] == [
        (0.0, 0.0, "executions", "AzureFunctionsFlexConsumption"),
        (pytest.approx(0.0000004), 250_000, "executions", "AzureFunctionsFlexConsumption")]


def test_app_service_sku_syncs_its_os_product():
    products = [
        _product("Azure App Service Premium v3 Plan - Linux", "P1 v3", "P1 v3 App",
                 [("0.155", "1 Hour", "0")]),
        _product("Azure App Service Premium v3 Plan", "P1 v3", "P1 v3 App",
                 [("0.315", "1 Hour", "0")]),
    ]
    rows = _sync("AppService-Linux-P1v3-Instance-Hour", products)
    assert [(r.price_usd, r.unit, r.service) for r in rows] == [(0.155, "hours", "AppService")]
