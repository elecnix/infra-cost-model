"""Flex Consumption always-ready instances and concurrency (#407).

Azure bills an always-ready instance for the time it holds its memory ready,
idle included, and once for the executions that share it. The
`alwaysReadyInstances` and `concurrency` settings say how many instances an
app keeps and how many executions one of them runs at once.
"""
import json
import warnings

import pytest

from infra_cost_model.engine.engine import CostEngine
from infra_cost_model.pricing.cache import SEED_PRICES_PATH
from infra_cost_model.resources.azure import AzureFunction
from infra_cost_model.resources.registry import extract_resources_from_arm

REGION = "eastus"
RG = "/subscriptions/0000/resourceGroups/rg/providers"
ADDRESS = "azurerm_linux_function_app.orders"
FLEX = {"hostingPlan": "flexConsumption"}

# 100,000 executions of 1 s on a 512 MB instance: 50,000 GB-seconds.
USAGE = {"invocations": {"unit": "requests", "value": 1},
         "avgDurationMs": {"unit": "ms", "value": 1000},
         "memoryMb": {"unit": "MB", "value": 512}}
PER_MONTH = 100_000
# One always-ready 512 MB instance for a 730-hour month: 365 GB-hours.
ALWAYS_READY_GB_HOURS = 365


def app_node(config, extra_usage=None):
    usage = dict(USAGE)
    usage.update(extra_usage or {})
    return ADDRESS, {
        "nodeType": "compute", "resourceAddress": ADDRESS, "provider": "azure",
        "service": "AzureFunctionsFlexConsumption", "region": REGION,
        "config": config, "usageMetrics": usage}


def compute(config, catalog, extra_usage=None, per_month=PER_MONTH):
    model = {"version": "1.0",
             "workflow": {"name": "w", "entry": ADDRESS,
                          "frequency": {"unit": "perMonth", "value": per_month}},
             "nodes": dict([app_node(config, extra_usage)]), "edges": []}
    engine = CostEngine(model, catalog=catalog, time_basis="monthly")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        costs = engine.compute()
    return costs, engine


def seeded(metric):
    rows = json.loads(SEED_PRICES_PATH.read_text())
    return [r for r in rows if r["usage_metric"] == metric and r["region"] == REGION]


def derived(config, **usage):
    values = {name: metric["value"] for name, metric in USAGE.items()}
    values.update(usage)
    return AzureFunction().derive_catalog_usage(values, config).quantities


# --- Always-ready instances ------------------------------------------------------------


def test_an_app_without_the_settings_bills_on_demand_executions():
    assert derived(FLEX) == {"AzureFunctionsFlex-Execution": 1.0,
                             "AzureFunctionsFlex-GB-Second": 0.5}


def test_always_ready_instances_bill_the_always_ready_rates():
    quantities = derived({**FLEX, "alwaysReadyInstances": 1})
    assert quantities == {"AzureFunctionsFlex-AlwaysReady-Execution": 1.0,
                          "AzureFunctionsFlex-AlwaysReady-GB-Second": 0.5}


def test_the_always_ready_baseline_is_a_fixed_gb_hour_metric():
    assert AzureFunction().catalog_metrics_for(FLEX) == {
        "alwaysReadyGBHours": {"AzureFunctionsFlex-AlwaysReady-Baseline-GB-Second": 3600}}
    # Another plan has no always-ready instances to bill.
    assert AzureFunction().catalog_metrics_for({"hostingPlan": "dedicated"}) == {}


def test_an_always_ready_instance_bills_the_baseline_plus_its_executions(seed_catalog):
    """1 always-ready 512 MB instance for a month.

    Executions $0.40 (1,000,000 at $0.000004 per 10), execution time $8.00
    (500,000 GB-s at $0.000016) and the baseline $5.256 (365 GB-hours, or
    1,314,000 GB-seconds, at $0.000004).
    """
    config = {**FLEX, "alwaysReadyInstances": 1}
    extra = {"alwaysReadyGBHours": {"unit": "GB-hours", "value": ALWAYS_READY_GB_HOURS,
                                    "fixed": True}}
    costs, engine = compute(config, seed_catalog, extra, per_month=1_000_000)
    assert costs[ADDRESS] == pytest.approx(0.40 + 8.00 + 5.256)
    assert engine.unpriced_metrics == []


def test_the_always_ready_meters_have_seed_rows():
    assert [r["price_usd"] for r in seeded("AzureFunctionsFlex-AlwaysReady-Execution")] \
        == [4e-07]
    assert [r["price_usd"] for r in seeded("AzureFunctionsFlex-AlwaysReady-GB-Second")] \
        == [1.6e-05]
    assert [r["price_usd"]
            for r in seeded("AzureFunctionsFlex-AlwaysReady-Baseline-GB-Second")] == [4e-06]


def test_an_arm_app_states_its_always_ready_instances():
    template = {"resources": [
        {"type": "Microsoft.Web/serverfarms", "name": "plan", "location": REGION,
         "sku": {"name": "FC1", "tier": "FlexConsumption", "capacity": 1}},
        {"type": "Microsoft.Web/sites", "name": "func", "kind": "functionapp",
         "location": REGION, "properties": {
             "serverFarmId": "[resourceId('Microsoft.Web/serverfarms', 'plan')]",
             "siteConfig": {"alwaysReady": [{"name": "functionApp", "instanceCount": 2},
                                            {"name": "orders", "instanceCount": 1}]}}}]}
    nodes = extract_resources_from_arm(template)
    assert nodes["Microsoft.Web/sites:func"]["config"]["alwaysReadyInstances"] == 3


def test_an_arm_app_without_always_ready_instances_keeps_its_config():
    template = {"resources": [
        {"type": "Microsoft.Web/serverfarms", "name": "plan", "location": REGION,
         "sku": {"name": "FC1", "tier": "FlexConsumption"}},
        {"type": "Microsoft.Web/sites", "name": "func", "kind": "functionapp",
         "location": REGION, "properties": {
             "serverFarmId": "[resourceId('Microsoft.Web/serverfarms', 'plan')]"}}]}
    nodes = extract_resources_from_arm(template)
    assert "alwaysReadyInstances" not in nodes["Microsoft.Web/sites:func"]["config"]


# --- Concurrency ------------------------------------------------------------------------


def test_concurrency_divides_the_billed_instance_time():
    assert derived({**FLEX, "concurrency": 4})["AzureFunctionsFlex-GB-Second"] == 0.125
    # Each execution keeps its own duration at least 100 ms.
    assert derived({**FLEX, "concurrency": 4},
                   avgDurationMs=50)["AzureFunctionsFlex-GB-Second"] == 0.0125


def test_concurrency_below_one_is_ignored():
    assert derived({**FLEX, "concurrency": 0}) == derived(FLEX)
    assert derived({**FLEX, "concurrency": "2"}) == derived(FLEX)


def test_two_concurrent_executions_cost_less_than_two_separate_instances(seed_catalog):
    """One instance runs both executions at once, so it bills one instance-time."""
    one_instance, _ = compute({**FLEX, "concurrency": 2}, seed_catalog, per_month=1_000_000)
    two_instances, _ = compute(FLEX, seed_catalog, per_month=1_000_000)
    # 1,000,000 executions of 1 s on a 512 MB instance: 500,000 GB-s when each
    # runs on its own instance, 250,000 when two share one. Both pay $0.30 over
    # the free executions.
    assert two_instances[ADDRESS] == pytest.approx(0.30 + 400_000 * 0.000026)
    assert one_instance[ADDRESS] == pytest.approx(0.30 + 150_000 * 0.000026)
    assert one_instance[ADDRESS] < two_instances[ADDRESS]


def test_concurrency_keeps_the_always_ready_baseline(seed_catalog):
    config = {**FLEX, "alwaysReadyInstances": 1, "concurrency": 2}
    extra = {"alwaysReadyGBHours": {"unit": "GB-hours", "value": ALWAYS_READY_GB_HOURS,
                                    "fixed": True}}
    costs, _ = compute(config, seed_catalog, extra, per_month=1_000_000)
    # The baseline is capacity the instance holds ready: concurrency does not
    # change it, only the executions' time shrinks.
    assert costs[ADDRESS] == pytest.approx(0.40 + 4.00 + 5.256)


def test_an_always_ready_app_never_bills_the_on_demand_meters(seed_catalog):
    config = {**FLEX, "alwaysReadyInstances": 1}
    extra = {"alwaysReadyGBHours": {"unit": "GB-hours", "value": ALWAYS_READY_GB_HOURS,
                                    "fixed": True}}
    costs, engine = compute(config, seed_catalog, extra, per_month=1_000_000)
    # The on-demand meters would charge $10.40 of execution time instead.
    assert costs[ADDRESS] == pytest.approx(0.40 + 500_000 * 0.000016 + 5.256)
    assert engine.unpriced_metrics == []