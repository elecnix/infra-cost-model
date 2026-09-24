"""Seed rows for the Azure handlers (#363).

The seed catalog prices the five Azure handlers in `eastus`, so an Azure
cost model has a price offline. Each price comes from the Azure Retail
Prices API (https://prices.azure.com/api/retail/prices), which the row's
`source` names with the meter ID.
"""
import json
import warnings

import pytest

from infra_cost_model.engine.engine import CostEngine
from infra_cost_model.pricing.cache import SEED_PRICES_PATH, load_seed_rows
from infra_cost_model.pricing.free_tiers import ACCOUNT, REGION as REGIONAL, free_tier_scope
from infra_cost_model.resources.azure import (
    APIManagement, AzureBlobStorage, AzureFunction, AzureOpenAI, CosmosDB,
)

REGION = "eastus"

# Each handler, its service, and the catalog metrics it prices.
HANDLER_METRICS = [
    (AzureFunction, "AzureFunctions",
     {"AzureFunctions-Execution", "AzureFunctions-GB-Second"}),
    (CosmosDB, "CosmosDB", {"CosmosDB-Serverless-RU", "CosmosDB-Storage-GB-Month"}),
    (APIManagement, "APIManagement", {"APIM-Consumption-Call"}),
    (AzureOpenAI, "AzureOpenAI", {"AzureOpenAI-Input-Token", "AzureOpenAI-Output-Token"}),
    (AzureBlobStorage, "BlobStorage", {
        "Blob-Hot-LRS-GB-Month", "Blob-Hot-Read-Operation",
        "Blob-Hot-LRS-Write-Operation"}),
]


def azure_rows():
    return [r for r in load_seed_rows() if r.vendor == "azure"]


def handler_catalog_metrics(handler) -> set[str]:
    metrics = set(handler().catalog_metrics.values())
    derived = handler().derive_catalog_usage(
        {"invocations": 1, "avgDurationMs": 100, "memoryMb": 128})
    if derived is not None:
        metrics |= set(derived.quantities)
    return metrics


@pytest.mark.parametrize("handler,service,metrics", HANDLER_METRICS,
                         ids=[h.__name__ for h, _, _ in HANDLER_METRICS])
def test_handler_metrics_have_seed_rows(handler, service, metrics):
    assert handler_catalog_metrics(handler) == metrics
    seeded = {r.usage_metric for r in azure_rows()
              if r.service == service and r.region == REGION}
    assert metrics <= seeded


def test_every_azure_row_belongs_to_a_handler_metric():
    known = {(service, m) for _, service, metrics in HANDLER_METRICS for m in metrics}
    for row in azure_rows():
        assert (row.service, row.usage_metric) in known, row


def test_azure_rows_cite_the_retail_prices_api():
    # load_seed_rows sets the source to "seed", so read the file's own fields.
    rows = [r for r in json.loads(SEED_PRICES_PATH.read_text()) if r["vendor"] == "azure"]
    assert rows
    for row in rows:
        assert row["source"].startswith("https://prices.azure.com/api/retail/prices?"), row
        assert "meterId" in row["source"], row
        assert row["effective_date"], row


@pytest.mark.parametrize("service,metric,quantity,cost", [
    # 1M free executions a month, then $0.20 per million.
    ("AzureFunctions", "AzureFunctions-Execution", 1_000_000, 0.0),
    ("AzureFunctions", "AzureFunctions-Execution", 3_000_000, 0.40),
    # 400,000 free GB-seconds a month, then $0.000016 per GB-second.
    ("AzureFunctions", "AzureFunctions-GB-Second", 400_000, 0.0),
    ("AzureFunctions", "AzureFunctions-GB-Second", 1_400_000, 16.0),
    # 1M free calls a month, then $3.50 per million.
    ("APIManagement", "APIM-Consumption-Call", 1_000_000, 0.0),
    ("APIManagement", "APIM-Consumption-Call", 3_000_000, 7.0),
    ("CosmosDB", "CosmosDB-Serverless-RU", 4_000_000, 1.0),
    ("CosmosDB", "CosmosDB-Storage-GB-Month", 10, 2.5),
    ("BlobStorage", "Blob-Hot-LRS-GB-Month", 100, 2.08),
    ("BlobStorage", "Blob-Hot-Read-Operation", 1_000_000, 0.40),
    ("BlobStorage", "Blob-Hot-LRS-Write-Operation", 1_000_000, 5.0),
    ("AzureOpenAI", "AzureOpenAI-Input-Token", 1_000_000, 2.50),
    ("AzureOpenAI", "AzureOpenAI-Output-Token", 1_000_000, 10.0),
])
def test_seed_prices(seed_catalog, service, metric, quantity, cost):
    result = seed_catalog.query("azure", service, REGION, metric, quantity)
    assert result is not None
    assert result.total_cost == pytest.approx(cost)


def test_functions_free_grant_covers_the_subscription():
    """The grant covers all function apps in a subscription, in every region."""
    for metric in ("AzureFunctions-Execution", "AzureFunctions-GB-Second"):
        assert free_tier_scope("azure", "AzureFunctions", metric) == ACCOUNT
    assert free_tier_scope("azure", "APIManagement", "APIM-Consumption-Call") == REGIONAL


def one_node_model(address, service, node_type, usage, per_month):
    return {
        "version": "1.0",
        "workflow": {"name": "azure", "entry": address,
                     "frequency": {"unit": "perMonth", "value": per_month}},
        "nodes": {address: {
            "nodeType": node_type, "resourceAddress": address, "provider": "azure",
            "service": service, "region": REGION, "usageMetrics": usage,
        }},
        "edges": [],
    }


def compute(model, catalog):
    engine = CostEngine(model, catalog=catalog, time_basis="monthly")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        costs = engine.compute()
    return costs, engine


def test_function_app_bills_executions_and_gb_seconds(seed_catalog):
    """3M executions of 1 s at 512 MB: 1.5M GB-seconds.

    (3M - 1M) x $0.20/M = $0.40, and (1.5M - 400K) x $0.000016 = $17.60.
    """
    address = "azurerm_linux_function_app.orders"
    model = one_node_model(address, "AzureFunctions", "compute", {
        "invocations": {"unit": "requests", "value": 1},
        "avgDurationMs": {"unit": "ms", "value": 1000},
        "memoryMb": {"unit": "MB", "value": 512},
    }, 3_000_000)
    costs, engine = compute(model, seed_catalog)
    assert costs[address] == pytest.approx(18.0)
    assert engine.unpriced_metrics == []


def test_openai_tokens_are_priced(seed_catalog):
    address = "azurerm_cognitive_account.oai"
    model = one_node_model(address, "AzureOpenAI", "compute", {
        "inputTokens": {"unit": "tokens", "value": 1000},
        "outputTokens": {"unit": "tokens", "value": 500},
    }, 1000)
    costs, engine = compute(model, seed_catalog)
    # 1M input tokens at $2.50/M and 500K output tokens at $10/M.
    assert costs[address] == pytest.approx(7.5)
    assert engine.unpriced_metrics == []
