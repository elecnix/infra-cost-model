"""Azure handlers price the product that the resource settings select.

Cosmos DB derives request units from reads and writes (#374). Blob Storage
reads the access tier and the redundancy, API Management its tier, and
Cosmos DB its capacity mode (#375). A setting whose product has no catalog
rows gets a warning at extraction.
"""
import json
import warnings

import pytest

from infra_cost_model.engine.engine import CostEngine
from infra_cost_model.pricing.cache import SEED_PRICES_PATH
from infra_cost_model.pricing.sources import infracost as ic
from infra_cost_model.resources.azure import APIManagement, AzureBlobStorage, CosmosDB
from infra_cost_model.resources.registry import (
    extract_resources_from_arm, extract_resources_from_pulumi, extract_resources_from_tf,
)

REGION = "eastus"


def seeded(service):
    rows = json.loads(SEED_PRICES_PATH.read_text())
    return {r["usage_metric"] for r in rows if r["service"] == service and r["region"] == REGION}


def one_node(address, service, node_type, usage, config, per_month=1):
    node = {"nodeType": node_type, "resourceAddress": address, "provider": "azure",
            "service": service, "region": REGION, "usageMetrics": usage}
    if config is not None:
        node["config"] = config
    return {"version": "1.0",
            "workflow": {"name": "w", "entry": address,
                         "frequency": {"unit": "perMonth", "value": per_month}},
            "nodes": {address: node}, "edges": []}


def compute(model, catalog):
    engine = CostEngine(model, catalog=catalog, time_basis="monthly")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        costs = engine.compute()
    return costs, engine


# --- Cosmos DB request units (#374) --------------------------------------------


def test_reads_and_writes_become_request_units():
    derived = CosmosDB().derive_catalog_usage(
        {"readRequests": 10.0, "writeRequests": 2.0}, {})
    # 1 RU per point read of 1 KB, 5 RU per write of 1 KB.
    assert derived.quantities == {"CosmosDB-Serverless-RU": 10.0 + 2 * 5.0}
    assert derived.consumed == {"readRequests", "writeRequests"}


def test_request_units_per_operation_are_configurable():
    derived = CosmosDB().derive_catalog_usage(
        {"readRequests": 10.0, "writeRequests": 2.0, "requestUnits": 3.0},
        {"ruPerRead": 2.5, "ruPerWrite": 12})
    assert derived.quantities == {"CosmosDB-Serverless-RU": 25.0 + 24.0 + 3.0}
    assert derived.consumed == {"readRequests", "writeRequests", "requestUnits"}


def test_storage_alone_derives_nothing():
    assert CosmosDB().derive_catalog_usage({"storageGb": 5.0}, {}) is None


def test_serverless_reads_and_writes_are_priced(seed_catalog):
    # 3M reads (3M RU) and 1M writes (5M RU): 8M RU at $0.25 per million.
    address = "azurerm_cosmosdb_account.orders"
    model = one_node(address, "CosmosDB", "storage", {
        "readRequests": {"unit": "requests", "value": 3},
        "writeRequests": {"unit": "requests", "value": 1},
    }, None, per_month=1_000_000)
    costs, engine = compute(model, seed_catalog)
    assert costs[address] == pytest.approx(2.0)
    assert engine.unpriced_metrics == []


def test_provisioned_account_bills_throughput_not_operations(seed_catalog):
    # 400 RU/s for a 730-hour month: 4 x 730 hours of 100 RU/s at $0.008.
    address = "azurerm_cosmosdb_account.orders"
    model = one_node(address, "CosmosDB", "storage", {
        "readRequests": {"unit": "requests", "value": 3},
        "throughputHours": {"unit": "hours", "value": 4 * 730, "fixed": True},
    }, {"capacityMode": "provisioned"}, per_month=1_000_000)
    costs, engine = compute(model, seed_catalog)
    assert costs[address] == pytest.approx(4 * 730 * 0.008)
    assert engine.unpriced_metrics == []


def test_multi_region_writes_select_their_throughput_rows():
    metrics = CosmosDB().catalog_metrics_for(
        {"capacityMode": "provisioned", "multiRegionWrites": True})
    assert metrics["throughputHours"] == "CosmosDB-Provisioned-MultiRegionWrite-100RU-Hour"
    assert metrics["throughputHours"] in seeded("CosmosDB")


@pytest.mark.parametrize("capabilities,mode", [
    ([{"name": "EnableServerless"}], "serverless"), ([], "provisioned"),
])
def test_terraform_capacity_mode(capabilities, mode):
    resource = {"address": "azurerm_cosmosdb_account.db", "type": "azurerm_cosmosdb_account",
                "values": {"location": REGION, "offer_type": "Standard",
                           "capabilities": capabilities,
                           "multiple_write_locations_enabled": False}}
    node = extract_resources_from_tf({"resource": [resource]})["azurerm_cosmosdb_account.db"]
    assert node["config"]["capacityMode"] == mode
    assert node["config"]["multiRegionWrites"] is False


def test_arm_capacity_mode():
    template = {"resources": [{
        "type": "Microsoft.DocumentDB/databaseAccounts", "name": "db", "location": REGION,
        "properties": {"databaseAccountOfferType": "Standard",
                       "capabilities": [{"name": "EnableServerless"}]}}]}
    node = extract_resources_from_arm(template)["Microsoft.DocumentDB/databaseAccounts:db"]
    assert node["config"]["capacityMode"] == "serverless"


# --- Blob Storage access tier and redundancy (#375) ----------------------------


def test_blob_defaults_to_hot_lrs():
    assert AzureBlobStorage().catalog_metrics_for({}) == AzureBlobStorage().catalog_metrics
    metrics = AzureBlobStorage().catalog_metrics
    assert metrics["storageGb"] == "Blob-Hot-LRS-GB-Month"
    assert metrics["readRequests"] == "Blob-Hot-Read-Operation"


@pytest.mark.parametrize("tier,replication,storage", [
    ("Cool", "GRS", "Blob-Cool-GRS-GB-Month"),
    ("cold", "zrs", "Blob-Cold-ZRS-GB-Month"),
    ("Hot", "RAGRS", "Blob-Hot-RAGRS-GB-Month"),
    ("Hot", "RA-GRS", "Blob-Hot-RAGRS-GB-Month"),
    ("Hot", "RA_GRS", "Blob-Hot-RAGRS-GB-Month"),
    ("Archive", "LRS", "Blob-Archive-LRS-GB-Month"),
])
def test_blob_settings_select_the_rows(tier, replication, storage):
    metrics = AzureBlobStorage().catalog_metrics_for(
        {"accessTier": tier, "replicationType": replication})
    assert metrics["storageGb"] == storage
    rows = seeded("BlobStorage")
    for name in ("storageGb", "readRequests", "writeRequests"):
        assert metrics[name] in rows


def test_every_blob_metric_has_a_descriptor():
    for metric in seeded("BlobStorage"):
        assert ic.METRIC_DESCRIPTORS[metric]["store_service"] == "BlobStorage"


def test_cool_blob_is_priced(seed_catalog):
    # 100 GB of Cool LRS at $0.0152 and 1M writes at $0.10 per 10K.
    address = "azurerm_storage_account.logs"
    model = one_node(address, "BlobStorage", "storage", {
        "storageGb": {"unit": "GB", "value": 100, "fixed": True},
        "writeRequests": {"unit": "requests", "value": 1},
    }, {"accessTier": "Cool", "replicationType": "LRS"}, per_month=1_000_000)
    costs, engine = compute(model, seed_catalog)
    assert costs[address] == pytest.approx(1.52 + 10.0)
    assert engine.unpriced_metrics == []


@pytest.mark.parametrize("values,match", [
    ({"account_tier": "Premium", "account_replication_type": "LRS"}, "Premium"),
    ({"account_tier": "Standard", "account_replication_type": "RAGZRS",
      "access_tier": "Hot"}, "RA-GZRS"),
])
def test_blob_settings_without_rows_warn(values, match):
    resource = {"address": "azurerm_storage_account.s", "type": "azurerm_storage_account",
                "values": {"location": REGION, **values}}
    with pytest.warns(UserWarning, match=rf"azurerm_storage_account.s.*{match}"):
        extract_resources_from_tf({"resource": [resource]})


def test_pulumi_azure_native_storage_account():
    stack = {"deployment": {"resources": [{
        "id": "/subscriptions/0/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/s",
        "type": "azure-native:storage:StorageAccount",
        "inputs": {"location": REGION, "sku": {"name": "Standard_GRS"}, "accessTier": "Cool",
                   "kind": "StorageV2"}}]}}
    node = next(iter(extract_resources_from_pulumi(stack).values()))
    assert node["config"]["accountTier"] == "Standard"
    assert node["config"]["replicationType"] == "GRS"
    assert node["config"]["accessTier"] == "Cool"


# --- API Management tier (#375) -------------------------------------------------


def test_consumption_tier_prices_calls():
    metrics = APIManagement().catalog_metrics_for({"skuName": "Consumption_0"})
    assert metrics["requests"] == "APIM-Consumption-Call"
    assert "unitHours" not in metrics


@pytest.mark.parametrize("sku,tier", [
    ("Developer_1", "Developer"), ("Basic_2", "Basic"), ("Standard_1", "Standard"),
    ("Premium_3", "Premium"), ("BasicV2_1", "BasicV2"), ("StandardV2_1", "StandardV2"),
])
def test_dedicated_tiers_bill_unit_hours(sku, tier):
    metrics = APIManagement().catalog_metrics_for({"skuName": sku})
    assert metrics["unitHours"] == f"APIM-{tier}-Unit-Hour"
    assert metrics["unitHours"] in seeded("APIManagement")


def test_classic_tier_includes_its_calls(seed_catalog):
    # Standard: 2 units for a 730-hour month at $0.9407 an hour. The calls
    # cost nothing beyond the units.
    address = "azurerm_api_management.api"
    model = one_node(address, "APIManagement", "routing", {
        "requests": {"unit": "requests", "value": 1},
        "unitHours": {"unit": "hours", "value": 2 * 730, "fixed": True},
    }, {"skuName": "Standard_2"}, per_month=5_000_000)
    costs, engine = compute(model, seed_catalog)
    assert costs[address] == pytest.approx(2 * 730 * 0.9407)
    assert engine.unpriced_metrics == []


def test_v2_tier_bills_calls_over_its_allowance(seed_catalog):
    # Basic v2 includes 10M calls a month, then $0.03 per 10K.
    address = "azurerm_api_management.api"
    model = one_node(address, "APIManagement", "routing", {
        "requests": {"unit": "requests", "value": 1},
    }, {"skuName": "BasicV2_1"}, per_month=20_000_000)
    costs, _ = compute(model, seed_catalog)
    assert costs[address] == pytest.approx(30.0)


def test_unknown_apim_tier_warns():
    resource = {"address": "azurerm_api_management.api", "type": "azurerm_api_management",
                "values": {"location": REGION, "sku_name": "Gold_1"}}
    with pytest.warns(UserWarning, match=r"azurerm_api_management.api.*Gold"):
        extract_resources_from_tf({"resource": [resource]})


# --- Infracost descriptors --------------------------------------------------------


def _sync(metric, products):
    """Sync ``metric`` from a fake Cloud Pricing API that has ``products``."""
    from unittest.mock import MagicMock, patch

    def post(url, headers=None, json=None, timeout=None):
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


def _azure_product(product, sku, meter, prices):
    return {"productFamily": "x",
            "attributes": [{"key": "productName", "value": product},
                           {"key": "skuName", "value": sku},
                           {"key": "meterName", "value": meter}],
            "prices": [{"USD": usd, "unit": unit, "startUsageAmount": start,
                        "endUsageAmount": None} for usd, unit, start in prices]}


def test_apim_v2_calls_sync_per_call_with_the_allowance():
    products = [
        _azure_product("API Management", "Basic v2", "Basic v2 Calls",
                       [("0", "10K", "0"), ("0.03", "10K", "1000")]),
        _azure_product("API Management", "Standard v2", "Standard v2 Calls",
                       [("0", "10K", "0"), ("0.025", "10K", "5000")]),
    ]
    rows = _sync("APIM-BasicV2-Call", products)
    assert [(r.price_usd, r.start_usage_amount, r.unit) for r in rows] == [
        (0.0, 0.0, "requests"), (pytest.approx(0.000003), 10_000_000, "requests")]
    assert {r.service for r in rows} == {"APIManagement"}


def test_cosmos_throughput_syncs_per_hour():
    products = [
        _azure_product("Azure Cosmos DB", "RUs", "100 RU/s", [("0.008", "1/Hour", "0")]),
        _azure_product("Azure Cosmos DB", "mRUs", "100 Multi-master RU/s",
                       [("0.016", "1/Hour", "0")]),
    ]
    rows = _sync("CosmosDB-Provisioned-100RU-Hour", products)
    assert [(r.price_usd, r.unit, r.service) for r in rows] == [(0.008, "hours", "CosmosDB")]


def test_every_settings_descriptor_names_a_seeded_metric():
    for metric, descriptor in ic.METRIC_DESCRIPTORS.items():
        if metric in ic._SETTINGS_METERS:
            assert metric in seeded(descriptor["store_service"]), metric


def test_standard_account_from_an_sku_doesnt_warn():
    """ARM and azure-native give `Standard_GRS`: the account tier, then the redundancy."""
    template = {"resources": [{
        "type": "Microsoft.Storage/storageAccounts", "name": "s", "location": REGION,
        "kind": "StorageV2", "sku": {"name": "Standard_RAGRS"},
        "properties": {"accessTier": "Cool"}}]}
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        node = extract_resources_from_arm(template)["Microsoft.Storage/storageAccounts:s"]
    assert node["config"] == {"accountTier": "Standard", "replicationType": "RAGRS",
                              "accessTier": "Cool"}
    assert AzureBlobStorage().catalog_metrics_for(node["config"])["storageGb"] == (
        "Blob-Cool-RAGRS-GB-Month")


def test_premium_account_is_unpriced_not_priced_as_standard():
    metrics = AzureBlobStorage().catalog_metrics_for(
        {"accountTier": "Premium", "replicationType": "LRS"})
    assert metrics["storageGb"] == "Blob-Premium-Hot-LRS-GB-Month"
    assert metrics["storageGb"] not in seeded("BlobStorage")


def test_shared_meters_select_their_own_sku():
    """Azure bills Hot GRS reads on the meter that Hot LRS reads use."""
    descriptor = ic.METRIC_DESCRIPTORS["Blob-Hot-GRS-Read-Operation"]
    filters = {f["key"]: f["value"] for f in descriptor["attribute_filters"]}
    assert filters == {"productName": "General Block Blob v2", "skuName": "Hot GRS",
                       "meterName": "Hot Read Operations"}


def test_null_values_dont_crash_the_storage_extractors():
    tf = {"address": "azurerm_storage_account.s", "type": "azurerm_storage_account",
          "values": None}
    assert AzureBlobStorage.extract_tf(tf).config["accessTier"] is None
    pulumi = {"id": "/subscriptions/0/resourceGroups/rg/providers/Microsoft.Storage/"
                    "storageAccounts/s", "inputs": None}
    assert AzureBlobStorage.extract_pulumi(pulumi).config["accountTier"] is None
