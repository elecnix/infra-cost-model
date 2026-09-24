"""Only consumption plan Function Apps get consumption rates (#382).

A Function App runs on the App Service plan its `serverFarmId` (ARM, Pulumi)
or `service_plan_id` (Terraform) points to. The plan SKU tells the hosting
plan apart: `Y1` or `Dynamic` is the consumption plan, `EP1` to `EP3` or
`ElasticPremium` is Elastic Premium, `FC1` is Flex Consumption, and any other
SKU is a dedicated plan. The engine prices only the consumption plan. The
other plans get their own service, so their usage is reported as unpriced
until #383 prices them.
"""
import warnings

import pytest

from infra_cost_model.engine.engine import CostEngine
from infra_cost_model.resources.registry import (
    extract_resources_from_arm, extract_resources_from_pulumi, extract_resources_from_tf,
)

# (sku name, sku tier, hostingPlan, service)
PLANS = [
    ("Y1", "Dynamic", "consumption", "AzureFunctions"),
    ("EP1", "ElasticPremium", "premium", "AzureFunctionsPremium"),
    ("EP3", "ElasticPremium", "premium", "AzureFunctionsPremium"),
    ("FC1", "FlexConsumption", "flexConsumption", "AzureFunctionsFlexConsumption"),
    ("B1", "Basic", "dedicated", "AppService"),
    ("S1", "Standard", "dedicated", "AppService"),
    ("P1v3", "PremiumV3", "dedicated", "AppService"),
]
NOT_CONSUMPTION = [plan for plan in PLANS if plan[2] != "consumption"]


def extract_quietly(extract, data):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        nodes = extract(data)
    return nodes, [str(w.message) for w in caught]


# ARM

def arm_template(sku, tier, server_farm_id="[resourceId('Microsoft.Web/serverfarms', 'plan-orders')]",
                 parameters=None):
    return {
        "parameters": parameters or {},
        "resources": [
            {"type": "Microsoft.Web/serverfarms", "name": "plan-orders", "location": "eastus",
             "sku": {"name": sku, "tier": tier}},
            {"type": "Microsoft.Web/sites", "name": "func-orders", "kind": "functionapp",
             "location": "eastus", "properties": {"serverFarmId": server_farm_id}},
        ],
    }


ARM_FUNC = "Microsoft.Web/sites:func-orders"


@pytest.mark.parametrize("sku,tier,plan,service", PLANS)
def test_arm_hosting_plan_from_serverfarm_sku(sku, tier, plan, service):
    nodes, _ = extract_quietly(extract_resources_from_arm, arm_template(sku, tier))
    node = nodes[ARM_FUNC]
    assert node["service"] == service
    assert node["config"]["hostingPlan"] == plan
    assert node["config"]["planSku"] == sku


@pytest.mark.parametrize("sku,tier,plan,service", NOT_CONSUMPTION)
def test_arm_other_plans_warn_they_are_unpriced(sku, tier, plan, service):
    _, messages = extract_quietly(extract_resources_from_arm, arm_template(sku, tier))
    assert any(ARM_FUNC in m and sku in m and "#383" in m for m in messages)


def test_arm_consumption_plan_does_not_warn_about_the_plan():
    _, messages = extract_quietly(extract_resources_from_arm, arm_template("Y1", "Dynamic"))
    assert not any(ARM_FUNC in m for m in messages)


def test_arm_tier_alone_tells_the_plan():
    nodes, _ = extract_quietly(extract_resources_from_arm, arm_template(None, "ElasticPremium"))
    assert nodes[ARM_FUNC]["config"]["hostingPlan"] == "premium"


@pytest.mark.parametrize("server_farm_id,parameters", [
    ("[resourceId('Microsoft.Web/serverfarms', parameters('planName'))]",
     {"planName": {"type": "string", "defaultValue": "plan-orders"}}),
    ("[resourceId('Microsoft.Web/serverfarms','plan-orders')]", None),
    ("/subscriptions/0000/resourceGroups/rg/providers/Microsoft.Web/serverfarms/plan-orders", None),
    ("[parameters('planId')]",
     {"planId": {"type": "string", "defaultValue":
                 "/subscriptions/0000/resourceGroups/rg/providers/Microsoft.Web/serverFarms/PLAN-ORDERS"}}),
])
def test_arm_server_farm_id_forms(server_farm_id, parameters):
    template = arm_template("EP1", "ElasticPremium", server_farm_id, parameters)
    nodes, _ = extract_quietly(extract_resources_from_arm, template)
    assert nodes[ARM_FUNC]["service"] == "AzureFunctionsPremium"


@pytest.mark.parametrize("server_farm_id", [
    "[resourceId('Microsoft.Web/serverfarms', 'plan-elsewhere')]",
    "[resourceId('Microsoft.Web/serverfarms', variables('planName'))]",
    None,
])
def test_arm_unknown_plan_is_priced_as_consumption_with_a_warning(server_farm_id):
    template = arm_template("EP1", "ElasticPremium", server_farm_id)
    nodes, messages = extract_quietly(extract_resources_from_arm, template)
    node = nodes[ARM_FUNC]
    assert node["service"] == "AzureFunctions"
    assert node["config"]["hostingPlan"] is None
    assert any(ARM_FUNC in m and "consumption" in m for m in messages)


# Terraform

PLAN_ID = "/subscriptions/0000/resourceGroups/rg/providers/Microsoft.Web/serverFarms/plan-orders"
TF_FUNC = "azurerm_linux_function_app.orders"


def tf_state(plan_values, app_values):
    return {"values": {"root_module": {"resources": [
        {"address": "azurerm_service_plan.orders", "type": "azurerm_service_plan",
         "values": plan_values},
        {"address": TF_FUNC, "type": "azurerm_linux_function_app",
         "values": {"location": "eastus", **app_values}},
    ]}}}


@pytest.mark.parametrize("sku,tier,plan,service", PLANS)
def test_terraform_hosting_plan_from_service_plan_sku(sku, tier, plan, service):
    state = tf_state({"id": PLAN_ID, "name": "plan-orders", "sku_name": sku},
                     {"service_plan_id": PLAN_ID})
    nodes, messages = extract_quietly(extract_resources_from_tf, state)
    node = nodes[TF_FUNC]
    assert node["service"] == service
    assert node["config"]["hostingPlan"] == plan
    assert node["config"]["planSku"] == sku
    warned = any(TF_FUNC in m and "#383" in m for m in messages)
    assert warned == (plan != "consumption")


def test_terraform_plan_matched_by_name_when_its_id_is_unknown():
    state = tf_state({"name": "plan-orders", "sku_name": "EP2"},
                     {"service_plan_id": PLAN_ID})
    nodes, _ = extract_quietly(extract_resources_from_tf, state)
    assert nodes[TF_FUNC]["config"]["hostingPlan"] == "premium"


def test_terraform_legacy_app_service_plan():
    state = {"values": {"root_module": {"resources": [
        {"address": "azurerm_app_service_plan.orders", "type": "azurerm_app_service_plan",
         "values": {"id": PLAN_ID, "name": "plan-orders",
                    "sku": [{"tier": "Standard", "size": "S1"}]}},
        {"address": "azurerm_function_app.orders", "type": "azurerm_function_app",
         "values": {"location": "eastus", "app_service_plan_id": PLAN_ID}},
    ]}}}
    nodes, _ = extract_quietly(extract_resources_from_tf, state)
    node = nodes["azurerm_function_app.orders"]
    assert node["service"] == "AppService"
    assert node["config"]["hostingPlan"] == "dedicated"


def test_terraform_unknown_plan_is_priced_as_consumption_with_a_warning():
    state = {"values": {"root_module": {"resources": [
        {"address": TF_FUNC, "type": "azurerm_linux_function_app",
         "values": {"location": "eastus", "service_plan_id": PLAN_ID}},
    ]}}}
    nodes, messages = extract_quietly(extract_resources_from_tf, state)
    assert nodes[TF_FUNC]["service"] == "AzureFunctions"
    assert any(TF_FUNC in m and "consumption" in m for m in messages)


# Pulumi

RG = "/subscriptions/0000/resourceGroups/rg/providers"


def test_pulumi_azure_native_premium_plan():
    stack = {"deployment": {"resources": [
        {"id": f"{RG}/Microsoft.Web/serverfarms/plan-orders",
         "type": "azure-native:web:AppServicePlan",
         "inputs": {"sku": {"name": "EP1", "tier": "ElasticPremium"}}},
        {"id": f"{RG}/Microsoft.Web/sites/func-orders", "type": "azure-native:web:WebApp",
         "inputs": {"kind": "functionapp", "location": "eastus",
                    "serverFarmId": f"{RG}/Microsoft.Web/serverfarms/plan-orders"}},
    ]}}
    nodes, _ = extract_quietly(extract_resources_from_pulumi, stack)
    node = nodes[f"{RG}/Microsoft.Web/sites/func-orders"]
    assert node["service"] == "AzureFunctionsPremium"
    assert node["config"]["hostingPlan"] == "premium"


def test_pulumi_classic_service_plan():
    stack = {"deployment": {"resources": [
        {"id": f"{RG}/Microsoft.Web/serverFarms/plan-orders",
         "type": "azure:appservice/servicePlan:ServicePlan",
         "inputs": {"skuName": "Y1"}},
        {"id": f"{RG}/Microsoft.Web/sites/func-orders",
         "type": "azure:appservice/linuxFunctionApp:LinuxFunctionApp",
         "inputs": {"location": "eastus",
                    "servicePlanId": f"{RG}/Microsoft.Web/serverFarms/plan-orders"}},
    ]}}
    nodes, _ = extract_quietly(extract_resources_from_pulumi, stack)
    node = nodes[f"{RG}/Microsoft.Web/sites/func-orders"]
    assert node["service"] == "AzureFunctions"
    assert node["config"]["hostingPlan"] == "consumption"


# Engine

def price(node, seed_catalog):
    address = node["resourceAddress"]
    model = {
        "version": "1.0",
        "workflow": {"name": "azure", "entry": address,
                     "frequency": {"unit": "perMonth", "value": 3_000_000}},
        "nodes": {address: {**node, "usageMetrics": {
            "invocations": {"unit": "requests", "value": 1},
            "avgDurationMs": {"unit": "ms", "value": 1000},
            "memoryMb": {"unit": "MB", "value": 512},
        }}},
        "edges": [],
    }
    engine = CostEngine(model, catalog=seed_catalog, time_basis="monthly")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        costs = engine.compute()
    return costs[address], engine


def extracted_node(sku, tier):
    nodes, _ = extract_quietly(extract_resources_from_arm, arm_template(sku, tier))
    return {key: value for key, value in nodes[ARM_FUNC].items() if key != "config"}


def test_consumption_app_is_priced_at_consumption_rates(seed_catalog):
    cost, engine = price(extracted_node("Y1", "Dynamic"), seed_catalog)
    # Same usage as test_azure_seed: $0.40 of executions and $17.60 of GB-seconds.
    assert cost == pytest.approx(18.0)
    assert engine.unpriced_metrics == []


@pytest.mark.parametrize("sku,tier", [("EP1", "ElasticPremium"), ("P1v3", "PremiumV3")])
def test_premium_and_dedicated_apps_are_not_priced_at_consumption_rates(sku, tier, seed_catalog):
    cost, engine = price(extracted_node(sku, tier), seed_catalog)
    assert cost == 0
    assert engine.unpriced_metrics != []
