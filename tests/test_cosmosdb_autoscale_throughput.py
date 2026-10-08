"""A Cosmos DB account prices the throughput its databases set (#399).

The `throughputHours` metric counts the hours of a month the account bills
throughput for, and the handler prices each of those hours at the throughput
the input's databases and containers set: `CosmosDB-Provisioned-100RU-Hour`,
`CosmosDB-Provisioned-MultiRegionWrite-100RU-Hour` or, for autoscale,
`CosmosDB-Autoscale-100RU-Hour`.
"""
import json
import warnings

import pytest

from infra_cost_model.engine.engine import CostEngine
from infra_cost_model.pricing.cache import SEED_PRICES_PATH
from infra_cost_model.pricing.sources import infracost as ic
from infra_cost_model.resources.azure import (
    CosmosDB, cosmos_throughput_from_tf, cosmos_units)
from infra_cost_model.resources.registry import (
    extract_resources_from_arm, extract_resources_from_pulumi, extract_resources_from_tf,
)

REGION = "eastus"
HOURS = 730

MANUAL = "CosmosDB-Provisioned-100RU-Hour"
MULTI_REGION = "CosmosDB-Provisioned-MultiRegionWrite-100RU-Hour"
AUTOSCALE = "CosmosDB-Autoscale-100RU-Hour"


def seeded(service="CosmosDB"):
    rows = json.loads(SEED_PRICES_PATH.read_text())
    return {r["usage_metric"]: r for r in rows
            if r["service"] == service and r["region"] == REGION}


def one_node(config, usage=None):
    address = "azurerm_cosmosdb_account.orders"
    node = {"nodeType": "storage", "resourceAddress": address, "provider": "azure",
            "service": "CosmosDB", "region": REGION,
            "usageMetrics": usage if usage is not None else {
                "throughputHours": {"unit": "hours", "value": HOURS, "fixed": True}},
            "config": config}
    return address, {"version": "1.0",
                     "workflow": {"name": "w", "entry": address,
                                  "frequency": {"unit": "perMonth", "value": 1}},
                     "nodes": {address: node}, "edges": []}


def compute(model, catalog):
    engine = CostEngine(model, catalog=catalog, time_basis="monthly")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        costs = engine.compute()
    return costs, engine


def account(**values):
    return {"address": "azurerm_cosmosdb_account.orders",
            "type": "azurerm_cosmosdb_account",
            "values": {"location": REGION, "offer_type": "Standard",
                       "name": "orders", **values}}


def sql_database(**values):
    return {"address": "azurerm_cosmosdb_sql_database.orders",
            "type": "azurerm_cosmosdb_sql_database",
            "values": {"account": "orders", "name": "orders", **values}}


def sql_container(**values):
    return {"address": "azurerm_cosmosdb_sql_container.items",
            "type": "azurerm_cosmosdb_sql_container",
            "values": {"account": "orders", "database": "orders", "name": "items",
                       **values}}


# --- Terraform extraction ------------------------------------------------------


def test_database_throughput_reaches_the_account():
    nodes = extract_resources_from_tf({"resource": [
        account(), sql_database(throughput=400)]})
    config = nodes["azurerm_cosmosdb_account.orders"]["config"]
    assert config["throughputRuPerSecond"] == 400
    assert config["autoscaleMaxRuPerSecond"] is None


def test_container_throughput_reaches_the_account():
    nodes = extract_resources_from_tf({"resource": [
        account(), sql_database(), sql_container(throughput=1000)]})
    config = nodes["azurerm_cosmosdb_account.orders"]["config"]
    assert config["throughputRuPerSecond"] == 1000


def test_a_shared_database_replaces_its_containers():
    """Azure bills a database's throughput once, shared by its containers."""
    nodes = extract_resources_from_tf({"resource": [
        account(), sql_database(throughput=400), sql_container(throughput=1000)]})
    config = nodes["azurerm_cosmosdb_account.orders"]["config"]
    assert config["throughputRuPerSecond"] == 400


def test_databases_without_throughput_add_nothing():
    nodes = extract_resources_from_tf({"resource": [
        account(), sql_database(), sql_container()]})
    config = nodes["azurerm_cosmosdb_account.orders"]["config"]
    assert config["throughputRuPerSecond"] is None


def test_throughput_of_another_account_is_left_out():
    nodes = extract_resources_from_tf({"resource": [
        account(), sql_database(throughput=400),
        {"address": "azurerm_cosmosdb_sql_database.billing", "type":
         "azurerm_cosmosdb_sql_database",
         "values": {"account": "billing", "name": "billing", "throughput": 4000}}]})
    config = nodes["azurerm_cosmosdb_account.orders"]["config"]
    assert config["throughputRuPerSecond"] == 400


def test_autoscale_settings_reach_the_account():
    nodes = extract_resources_from_tf({"resource": [
        account(), sql_database(autoscale_settings=[{"max_throughput": 4000}])]})
    config = nodes["azurerm_cosmosdb_account.orders"]["config"]
    assert config["autoscaleMaxRuPerSecond"] == 4000
    assert config["throughputRuPerSecond"] is None


def test_an_account_of_another_api_sets_no_throughput():
    """Only the SQL API databases and containers set a Cosmos DB throughput."""
    nodes = extract_resources_from_tf({"resource": [
        account(), {"address": "azurerm_cosmosdb_mongodb_database.billing",
                    "type": "azurerm_cosmosdb_mongodb_database",
                    "values": {"account": "orders", "name": "billing",
                               "throughput": 400}}]})
    config = nodes["azurerm_cosmosdb_account.orders"]["config"]
    assert config["throughputRuPerSecond"] is None


# --- Pulumi and ARM extraction -------------------------------------------------


def test_pulumi_sql_database_throughput():
    stack = {"deployment": {"resources": [
        {"id": "/subscriptions/0/resourceGroups/rg/providers/Microsoft.DocumentDB/"
                "databaseAccounts/orders",
         "type": "azure-native:documentdb:DatabaseAccount",
         "inputs": {"location": REGION, "databaseAccountOfferType": "Standard"}},
        {"id": "/subscriptions/0/resourceGroups/rg/providers/Microsoft.DocumentDB/"
                "databaseAccounts/orders/sqlDatabases/orders",
         "type": "azure-native:documentdb:DatabaseAccountSqlDatabase",
         "inputs": {"accountName": "orders", "databaseName": "orders",
                    "options": {"throughput": 1000}}}]}}
    node = next(n for a, n in extract_resources_from_pulumi(stack).items()
                if a.endswith("databaseAccounts/orders"))
    assert node["config"]["throughputRuPerSecond"] == 1000


def test_pulumi_classic_container_throughput():
    stack = {"deployment": {"resources": [
        {"id": "/subscriptions/0/resourceGroups/rg/providers/Microsoft.DocumentDB/"
                "databaseAccounts/orders",
         "type": "azure:cosmosdb:Account",
         "inputs": {"location": REGION, "name": "orders"}},
        {"id": "/subscriptions/0/resourceGroups/rg/providers/Microsoft.DocumentDB/"
                "databaseAccounts/orders/sqlDatabases/orders",
         "type": "azure:cosmosdb:SqlDatabase",
         "inputs": {"account": "orders", "name": "orders"}},
        {"id": "/subscriptions/0/resourceGroups/rg/providers/Microsoft.DocumentDB/"
                "databaseAccounts/orders/sqlDatabases/orders/containers/items",
         "type": "azure:cosmosdb:SqlContainer",
         "inputs": {"account": "orders", "database_name": "orders",
                    "container_name": "items",
                    "autoscale_settings": {"max_throughput": 4000}}}]}}
    node = next(n for a, n in extract_resources_from_pulumi(stack).items()
                if a.endswith("databaseAccounts/orders"))
    assert node["config"]["autoscaleMaxRuPerSecond"] == 4000


def test_arm_database_throughput():
    template = {"resources": [
        {"type": "Microsoft.DocumentDB/databaseAccounts", "name": "orders",
         "location": REGION, "properties": {"databaseAccountOfferType": "Standard"}},
        {"type": "Microsoft.DocumentDB/databaseAccounts/sqlDatabases", "name": "orders/db",
         "properties": {"options": {"throughput": 400}}}]}
    node = extract_resources_from_arm(template)["Microsoft.DocumentDB/databaseAccounts:orders"]
    assert node["config"]["throughputRuPerSecond"] == 400


def test_arm_container_autoscale_throughput():
    template = {"resources": [
        {"type": "Microsoft.DocumentDB/databaseAccounts", "name": "orders",
         "location": REGION, "properties": {"databaseAccountOfferType": "Standard"}},
        {"type": "Microsoft.DocumentDB/databaseAccounts/sqlDatabases", "name": "orders/db",
         "properties": {"options": {"throughput": 400}}},
        {"type": "Microsoft.DocumentDB/databaseAccounts/sqlDatabases/sqlContainers",
         "name": "orders/db/items", "properties": {
             "options": {"autoscaleSettings": {"maxThroughput": 4000}}}}]}
    node = extract_resources_from_arm(template)["Microsoft.DocumentDB/databaseAccounts:orders"]
    # The database's throughput is shared, so the autoscale container is left out.
    assert node["config"]["throughputRuPerSecond"] == 400
    assert node["config"]["autoscaleMaxRuPerSecond"] is None


def test_an_unnamed_database_warns_rather_than_costing_nothing():
    template = {"resources": [
        {"type": "Microsoft.DocumentDB/databaseAccounts", "name": "orders",
         "location": REGION, "properties": {"databaseAccountOfferType": "Standard"}},
        {"type": "Microsoft.DocumentDB/databaseAccounts/sqlDatabases", "name": "db",
         "properties": {"options": {"throughput": 400}}}]}
    with pytest.warns(UserWarning, match="name no account"):
        extract_resources_from_arm(template)


# --- Catalog metrics -----------------------------------------------------------


def test_manual_throughput_prices_each_hundred_request_units():
    metrics = CosmosDB().catalog_metrics_for(
        {"capacityMode": "provisioned", "throughputRuPerSecond": 400})
    assert metrics["throughputHours"] == {MANUAL: 4}


@pytest.mark.parametrize("config,metric,units", [
    ({"capacityMode": "provisioned", "throughputRuPerSecond": 400}, MANUAL, 4),
    ({"capacityMode": "provisioned", "autoscaleMaxRuPerSecond": 4000}, AUTOSCALE, 40),
    # Autoscale bills 1.5 times the manual rate on one meter, whatever the
    # number of write regions.
    ({"capacityMode": "provisioned", "autoscaleMaxRuPerSecond": 400,
      "multiRegionWrites": True}, AUTOSCALE, 4),
])
def test_autoscale_prices_the_autoscale_rows(config, metric, units):
    assert CosmosDB().catalog_metrics_for(config)["throughputHours"] == {metric: units}


def test_a_multi_region_account_keeps_its_write_rate():
    metrics = CosmosDB().catalog_metrics_for(
        {"capacityMode": "provisioned", "multiRegionWrites": True,
         "throughputRuPerSecond": 400})
    assert metrics["throughputHours"] == {MULTI_REGION: 4}


def test_a_hand_counted_model_keeps_counting_its_own_hours():
    """No throughput in `config`: the metric counts hours of 100 RU/s (#375)."""
    metrics = CosmosDB().catalog_metrics_for({"capacityMode": "provisioned"})
    assert metrics["throughputHours"] == MANUAL


def test_a_serverless_account_has_no_throughput_rows():
    metrics = CosmosDB().catalog_metrics_for(
        {"capacityMode": "serverless", "throughputRuPerSecond": 400})
    assert "throughputHours" not in metrics


# --- Prices --------------------------------------------------------------------


@pytest.mark.parametrize("config,metric,price", [
    ({"capacityMode": "provisioned", "throughputRuPerSecond": 400}, MANUAL, 0.008),
    ({"capacityMode": "provisioned", "multiRegionWrites": True,
      "throughputRuPerSecond": 400}, MULTI_REGION, 0.016),
    ({"capacityMode": "provisioned", "autoscaleMaxRuPerSecond": 4000}, AUTOSCALE, 0.012),
])
def test_throughput_is_priced(seed_catalog, config, metric, price):
    assert seeded()[metric]["price_usd"] == pytest.approx(price)
    address, model = one_node(config)
    costs, engine = compute(model, seed_catalog)
    units = config.get("throughputRuPerSecond", config.get("autoscaleMaxRuPerSecond")) / 100
    assert costs[address] == pytest.approx(units * HOURS * price)
    assert engine.unpriced_metrics == []


def test_autoscale_costs_more_than_manual_for_the_same_request_units(seed_catalog):
    _, manual = one_node({"capacityMode": "provisioned", "throughputRuPerSecond": 400})
    _, autoscale = one_node({"capacityMode": "provisioned", "autoscaleMaxRuPerSecond": 400})
    manual_cost = compute(manual, seed_catalog)[0]["azurerm_cosmosdb_account.orders"]
    autoscale_cost = compute(autoscale, seed_catalog)[0]["azurerm_cosmosdb_account.orders"]
    assert autoscale_cost == pytest.approx(1.5 * manual_cost)


def test_a_serverless_account_costs_no_throughput(seed_catalog):
    address, model = one_node({"capacityMode": "serverless", "throughputRuPerSecond": 400},
                              usage={})
    costs, engine = compute(model, seed_catalog)
    assert costs.get(address, 0.0) == 0.0
    assert engine.unpriced_metrics == []


def test_the_autoscale_descriptor_reads_the_autoscale_product():
    """The retail API has the autoscale product in no other SKU's name."""
    descriptor = ic.METRIC_DESCRIPTORS[AUTOSCALE]
    assert descriptor["azure_retail"] is True
    assert {f["key"]: f["value"] for f in descriptor["attribute_filters"]} == {
        "productName": "Azure Cosmos DB autoscale", "skuName": "AP1",
        "meterName": "AP1 100 RUs"}


def test_the_autoscale_descriptor_names_both_spellings_of_its_unit():
    """The retail API and Infracost spell this meter's unit differently.

    Naming one spelling leaves the sync matching no price, so it stores no
    autoscale rows at all and the seed row prices the metric offline alone.
    """
    descriptor = ic.METRIC_DESCRIPTORS[AUTOSCALE]
    assert descriptor["unit"] == ["1/Hour", "1 Hour"]
    assert descriptor["store_unit"] == "hours"


def test_an_account_with_autoscale_and_manual_throughput_bills_both():
    """Azure bills each database or container at its own rate.

    An autoscale database and a manual one in the same account bill the
    autoscale meter for the first and the manual meter for the second, so
    neither throughput is dropped.
    """
    metrics = CosmosDB().catalog_metrics_for(
        {"capacityMode": "provisioned", "autoscaleMaxRuPerSecond": 4000,
         "throughputRuPerSecond": 400})
    assert metrics["throughputHours"] == {AUTOSCALE: 40, MANUAL: 4}


def test_a_multi_region_account_bills_its_manual_part_at_the_write_rate():
    metrics = CosmosDB().catalog_metrics_for(
        {"capacityMode": "provisioned", "multiRegionWrites": True,
         "autoscaleMaxRuPerSecond": 1000, "throughputRuPerSecond": 400})
    assert metrics["throughputHours"] == {AUTOSCALE: 10, MULTI_REGION: 4}


def test_mixed_throughput_is_priced(seed_catalog):
    address, model = one_node({"capacityMode": "provisioned",
                               "autoscaleMaxRuPerSecond": 4000,
                               "throughputRuPerSecond": 400})
    costs, engine = compute(model, seed_catalog)
    rows = seeded()
    expected = HOURS * (40 * rows[AUTOSCALE]["price_usd"] + 4 * rows[MANUAL]["price_usd"])
    assert costs[address] == pytest.approx(expected)
    assert engine.unpriced_metrics == []


def test_a_declared_autoscale_maximum_of_zero_is_kept():
    """0 is a maximum the input declares, not the absence of one."""
    metrics = CosmosDB().catalog_metrics_for(
        {"capacityMode": "provisioned", "autoscaleMaxRuPerSecond": 0,
         "throughputRuPerSecond": 400})
    assert metrics["throughputHours"] == {AUTOSCALE: 0, MANUAL: 4}


def test_the_units_of_a_declared_autoscale_maximum_of_zero_are_zero():
    assert cosmos_units({"capacityMode": "provisioned",
                         "autoscaleMaxRuPerSecond": 0,
                         "throughputRuPerSecond": 400}) == 0


def test_every_throughput_metric_the_handler_names_is_priced():
    handler = CosmosDB()
    metrics = set()
    for config in ({"capacityMode": "provisioned", "throughputRuPerSecond": 400},
                   {"capacityMode": "provisioned", "autoscaleMaxRuPerSecond": 4000},
                   {"capacityMode": "provisioned", "multiRegionWrites": True},
                   {"capacityMode": "provisioned"}):
        value = handler.catalog_metrics_for(config)["throughputHours"]
        metrics |= set(value) if isinstance(value, dict) else {value}
    assert metrics <= set(seeded())


def test_the_database_reader_only_takes_sql_resources():
    entries = cosmos_throughput_from_tf([
        sql_database(throughput=400),
        {"address": "azurerm_cosmosdb_table.t", "type": "azurerm_cosmosdb_table",
         "values": {"account": "orders", "name": "t", "throughput": 400}}])
    assert [(e.name, e.ru_per_second) for e in entries] == [("orders", 400.0)]