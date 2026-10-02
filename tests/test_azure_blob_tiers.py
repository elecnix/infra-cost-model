"""Blob Storage bills data retrieval and early deletion in the cool tiers (#398).

The cool tiers bill a `dataRetrievalGb` metric per GB, and an
`earlyDeleteGb` metric for blobs deleted before the tier's minimum
retention (30, 90 or 180 days), prorated over that window. A Premium
account has rows of its own, a general-purpose v1 account reads the
product of its kind, and Azure publishes no write meter for Hot and Cool
RA-GZRS.
"""
import json
import warnings

import pytest

from infra_cost_model.engine.engine import CostEngine
from infra_cost_model.pricing.cache import SEED_PRICES_PATH
from infra_cost_model.pricing.sources import infracost as ic
from infra_cost_model.resources.azure import AzureBlobStorage
from infra_cost_model.resources.registry import extract_resources_from_tf

REGION = "eastus"


def seeded(service):
    rows = json.loads(SEED_PRICES_PATH.read_text())
    return {r["usage_metric"] for r in rows
            if r["service"] == service and r["region"] == REGION}


def price_of(metric, quantity=1):
    return next(r["price_usd"] for r in json.loads(SEED_PRICES_PATH.read_text())
                if r["usage_metric"] == metric and r["region"] == REGION
                and r.get("start_usage_amount", 0) == 0)


def one_node(address, usage, config, per_month=1):
    return {"version": "1.0",
            "workflow": {"name": "w", "entry": address,
                         "frequency": {"unit": "perMonth", "value": per_month}},
            "nodes": {address: {
                "nodeType": "storage", "resourceAddress": address, "provider": "azure",
                "service": "BlobStorage", "region": REGION, "usageMetrics": usage,
                "config": config}},
            "edges": []}


def compute(model, catalog):
    engine = CostEngine(model, catalog=catalog, time_basis="monthly")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        costs = engine.compute()
    return costs, engine


def extract(config_values):
    resource = {"address": "azurerm_storage_account.s",
                "type": "azurerm_storage_account",
                "values": {"location": REGION, **config_values}}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        extract_resources_from_tf({"resource": [resource]})
    return [str(w.message) for w in caught]


# --- Data retrieval --------------------------------------------------------------


def test_cool_tier_prices_data_retrieval(seed_catalog):
    address = "azurerm_storage_account.archive_logs"
    model = one_node(address, {
        "storageGb": {"unit": "GB", "value": 100, "fixed": True},
        "dataRetrievalGb": {"unit": "GB", "value": 1_000, "fixed": True},
    }, {"accessTier": "Cool", "replicationType": "LRS"})
    costs, engine = compute(model, seed_catalog)
    assert costs[address] == pytest.approx(
        100 * price_of("Blob-Cool-LRS-GB-Month")
        + 1_000 * price_of("Blob-Cool-LRS-Retrieval-GB"))
    assert engine.unpriced_metrics == []


@pytest.mark.parametrize("tier,replication", [
    ("Cool", "LRS"), ("Cool", "ZRS"), ("Cool", "GRS"), ("Cool", "RA-GRS"),
    ("Cool", "GZRS"), ("Cool", "RA-GZRS"),
    ("Cold", "LRS"), ("Cold", "ZRS"), ("Cold", "GRS"), ("Cold", "RA-GRS"),
    ("Cold", "GZRS"), ("Cold", "RA-GZRS"),
    ("Archive", "LRS"), ("Archive", "GRS"), ("Archive", "RA-GRS"),
])
def test_every_cool_tier_has_a_retrieval_metric(tier, replication):
    metric = AzureBlobStorage().catalog_metrics_for(
        {"accessTier": tier, "replicationType": replication})["dataRetrievalGb"]
    assert metric in seeded("BlobStorage")


def test_hot_tier_has_no_retrieval_metric():
    """Hot retrieval is free, so the Hot tier has no row to price it."""
    assert "dataRetrievalGb" not in AzureBlobStorage().catalog_metrics_for(
        {"accessTier": "Hot", "replicationType": "LRS"})


# --- Early deletion --------------------------------------------------------------


@pytest.mark.parametrize("tier,window", [("Cool", 30), ("Cold", 90), ("Archive", 180)])
def test_early_deletion_costs_the_tier_s_whole_window(seed_catalog, tier, window):
    """Azure prices an early deletion at the tier's storage price for the
    whole minimum-retention window."""
    address = "azurerm_storage_account.cold_blobs"
    model = one_node(address, {
        "storageGb": {"unit": "GB", "value": 1, "fixed": True},
        "earlyDeleteGb": {"unit": "GB", "value": 100, "fixed": True},
    }, {"accessTier": tier, "replicationType": "LRS"})
    costs, engine = compute(model, seed_catalog)
    assert costs[address] == pytest.approx(
        price_of(f"Blob-{tier}-LRS-GB-Month")
        + 100 * price_of(f"Blob-{tier}-LRS-Early-Delete-GB"))
    assert engine.unpriced_metrics == []
    assert price_of(f"Blob-{tier}-LRS-Early-Delete-GB") == price_of(
        f"Blob-{tier}-LRS-GB-Month")


@pytest.mark.parametrize("tier,replication,priced", [
    ("Cool", "LRS", True), ("Cool", "ZRS", True), ("Cool", "GRS", True),
    ("Cool", "RA-GRS", True), ("Cool", "GZRS", False), ("Cool", "RA-GZRS", False),
    ("Cold", "LRS", True), ("Cold", "RA-GZRS", True),
    ("Archive", "LRS", True), ("Archive", "RA-GRS", True),
])
def test_only_the_skus_with_an_early_delete_meter(tier, replication, priced):
    metrics = AzureBlobStorage().catalog_metrics_for(
        {"accessTier": tier, "replicationType": replication})
    assert ("earlyDeleteGb" in metrics) is priced
    if priced:
        assert metrics["earlyDeleteGb"] in seeded("BlobStorage")


def test_early_deletion_of_a_hot_account_is_not_a_metric():
    assert "earlyDeleteGb" not in AzureBlobStorage().catalog_metrics_for({})


# --- RA-GZRS writes --------------------------------------------------------------


@pytest.mark.parametrize("tier", ["Hot", "Cool"])
def test_ragzrs_writes_are_priced_and_do_not_warn(seed_catalog, tier):
    """Azure publishes no write meter for Hot or Cool RA-GZRS in the
    general-purpose v2 product, so those writes are not billed separately."""
    address = "azurerm_storage_account.zone_replicated"
    model = one_node(address, {
        "storageGb": {"unit": "GB", "value": 100, "fixed": True},
        "writeRequests": {"unit": "requests", "value": 1},
    }, {"accessTier": tier, "replicationType": "RAGZRS"}, per_month=1_000_000)
    costs, engine = compute(model, seed_catalog)
    assert extract({"account_replication_type": "RAGZRS", "access_tier": tier}) == []
    assert engine.unpriced_metrics == []
    assert costs[address] == pytest.approx(
        100 * price_of(f"Blob-{tier}-RAGZRS-GB-Month"))


def test_the_other_gzrs_skus_still_bill_writes():
    for tier in ("Hot", "Cool"):
        metrics = AzureBlobStorage().catalog_metrics_for(
            {"accessTier": tier, "replicationType": "GZRS"})
        assert metrics["writeRequests"] in seeded("BlobStorage")


def test_ragzrs_writes_are_not_mapped_to_a_meter():
    metrics = AzureBlobStorage().catalog_metrics_for(
        {"accessTier": "Hot", "replicationType": "RAGZRS"})
    assert "writeRequests" not in metrics


# --- Premium and general-purpose v1 accounts -------------------------------------


def test_premium_account_prices_above_a_hot_lrs_account(seed_catalog):
    address = "azurerm_storage_account.records"
    model = one_node(address, {
        "storageGb": {"unit": "GB", "value": 100, "fixed": True},
        "readRequests": {"unit": "requests", "value": 1},
        "writeRequests": {"unit": "requests", "value": 1},
    }, {"accountTier": "Premium", "replicationType": "LRS"}, per_month=1_000_000)
    costs, engine = compute(model, seed_catalog)
    hot = AzureBlobStorage().catalog_metrics["storageGb"]
    premium = AzureBlobStorage().catalog_metrics_for(
        {"accountTier": "Premium", "replicationType": "LRS"})["storageGb"]
    assert premium != hot
    assert costs[address] == pytest.approx(
        100 * price_of(premium)
        + 1_000_000 * price_of("Blob-Premium-LRS-Read-Operation")
        + 1_000_000 * price_of("Blob-Premium-LRS-Write-Operation"))
    assert costs[address] > 100 * price_of(hot)
    assert engine.unpriced_metrics == []


@pytest.mark.parametrize("replication", ["LRS", "ZRS"])
def test_premium_account_metrics_have_rows(replication):
    metrics = AzureBlobStorage().catalog_metrics_for(
        {"accountTier": "Premium", "replicationType": replication})
    for name in ("storageGb", "readRequests", "writeRequests"):
        assert metrics[name] in seeded("BlobStorage")


def test_premium_account_has_no_retrieval_rows():
    """A Premium account reads its blobs in place, with no retrieval charge."""
    assert "dataRetrievalGb" not in AzureBlobStorage().catalog_metrics_for(
        {"accountTier": "Premium", "replicationType": "LRS"})


@pytest.mark.parametrize("kind", ["Storage", "storagev2", "StorageV2"])
def test_general_purpose_v1_accounts_read_their_own_product(kind):
    metrics = AzureBlobStorage().catalog_metrics_for(
        {"accountKind": kind, "accessTier": "Cool", "replicationType": "LRS"})
    prefix = "Blob-Storage" if kind.lower() == "storage" else "Blob"
    assert metrics["storageGb"] == f"{prefix}-Cool-LRS-GB-Month"
    assert metrics["dataRetrievalGb"] == f"{prefix}-Cool-LRS-Retrieval-GB"
    assert metrics["storageGb"] in seeded("BlobStorage")


def test_general_purpose_v1_accounts_do_not_warn(seed_catalog):
    address = "azurerm_storage_account.legacy"
    model = one_node(address, {
        "storageGb": {"unit": "GB", "value": 100, "fixed": True},
        "dataRetrievalGb": {"unit": "GB", "value": 100, "fixed": True},
    }, {"accountKind": "Storage", "accessTier": "Cool", "replicationType": "LRS"})
    costs, engine = compute(model, seed_catalog)
    assert costs[address] == pytest.approx(
        100 * price_of("Blob-Storage-Cool-LRS-GB-Month")
        + 100 * price_of("Blob-Storage-Cool-LRS-Retrieval-GB"))
    assert engine.unpriced_metrics == []


def test_the_account_kind_selects_the_descriptor_product():
    v1 = ic.METRIC_DESCRIPTORS["Blob-Storage-Cool-LRS-Retrieval-GB"]
    v2 = ic.METRIC_DESCRIPTORS["Blob-Cool-LRS-Retrieval-GB"]
    products = lambda d: {f["key"]: f["value"] for f in d["attribute_filters"]}
    assert products(v1)["productName"] == "Blob Storage"
    assert products(v2)["productName"] == "General Block Blob v2"
    assert products(ic.METRIC_DESCRIPTORS["Blob-Premium-LRS-GB-Month"])["productName"] == (
        "Premium Block Blob")


@pytest.mark.parametrize("values,match", [
    ({"account_tier": "Premium", "account_replication_type": "GRS"}, "GRS"),
    ({"account_kind": "BlobStorage", "account_replication_type": "LRS"}, "BlobStorage"),
])
def test_settings_without_rows_still_warn(values, match):
    assert any(match in warning for warning in extract(values))