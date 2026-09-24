"""Infracost descriptors for the Azure and GCP handlers (#226).

The fake Cloud Pricing API below holds a slice of the products that the live
Infracost API returned on 2026-09-24 for Azure (eastus) and GCP (us-central1,
southamerica-east1 and the global catalogue). It answers a query the way the
live API does: it keeps the products of the vendor, service and region whose
product family and attributes equal the filter values.
"""

import warnings
from unittest.mock import MagicMock, patch

import pytest

from infra_cost_model.engine import CostEngine
from infra_cost_model.engine.engine import UnpricedMetricWarning
from infra_cost_model.pricing.cache import PricingCache
from infra_cost_model.pricing.catalog import PricingCatalog
from infra_cost_model.pricing.sources import infracost as ic
from infra_cost_model.resources.azure import (
    APIManagement, AzureBlobStorage, AzureFunction, CosmosDB,
)
from infra_cost_model.resources.gcp import CloudFunction, CloudRun, CloudStorage, Firestore


def _product(vendor, service, region, family, attrs, prices):
    return {
        "vendor": vendor, "service": service, "region": region,
        "productFamily": family,
        "attributes": [{"key": k, "value": v} for k, v in attrs.items()],
        "prices": [
            {"USD": usd, "unit": unit, "startUsageAmount": start, "endUsageAmount": end}
            for usd, unit, start, end in prices
        ],
    }


def _azure(service, family, product_name, sku, meter, prices, region="eastus"):
    # The live API gives Azure tiers a start and no end.
    return _product("azure", service, region, family, {
        "productName": product_name, "skuName": sku, "meterName": meter,
        "meterId": f"{product_name}/{sku}/{meter}",
    }, [(usd, unit, start, None) for usd, unit, start, _ in prices])


def _gcp(service, region, family, description, group, prices):
    return _product("gcp", service, region, family,
                    {"description": description, "resourceGroup": group}, prices)


AZURE_EASTUS = [
    _azure("Functions", "Compute", "Functions", "Standard", "Standard Total Executions",
           [("0", "10", "0", "100000"), ("0.000002", "10", "100000", None)]),
    _azure("Functions", "Compute", "Functions", "Standard", "Standard Execution Time",
           [("0", "1 GB Second", "0", "400000"), ("0.000016", "1 GB Second", "400000", None)]),
    _azure("Functions", "Compute", "Flex Consumption", "On Demand", "On Demand Total Executions",
           [("0", "10", "0", "25000"), ("0.000004", "10", "25000", None)]),
    _azure("Functions", "Compute", "Flex Consumption", "On Demand", "On Demand Execution Time",
           [("0", "1 GB Second", "0", "100000"), ("0.000026", "1 GB Second", "100000", None)]),
    _azure("Azure Cosmos DB", "Databases", "Azure Cosmos DB serverless", "RUs", "1M RUs",
           [("0.25", "1M", "0", None)]),
    _azure("Azure Cosmos DB", "Databases", "Azure Cosmos DB", "RUs", "Data Stored",
           [("0.25", "1 GB/Month", "0", None)]),
    _azure("Azure Cosmos DB", "Databases", "Azure Cosmos DB", "mRUs", "Data Stored",
           [("0.25", "1 GB/Month", "0", None)]),
    _azure("Azure Cosmos DB", "Databases", "Azure Cosmos DB", "RUs", "100 RU/s",
           [("0.008", "1/Hour", "0", None)]),
    _azure("API Management", "Developer Tools", "API Management", "Consumption",
           "Consumption Calls", [("0", "10K", "0", "100"), ("0.035", "10K", "100", None)]),
    _azure("API Management", "Developer Tools", "API Management", "Basic v2",
           "Basic v2 Calls", [("0", "10K", "0", "1000"), ("0.03", "10K", "1000", None)]),
    _azure("Storage", "Storage", "General Block Blob v2", "Hot LRS", "Hot LRS Data Stored", [
        ("0.0208", "1 GB/Month", "0", "51200"), ("0.019968", "1 GB/Month", "51200", "512000"),
        ("0.019136", "1 GB/Month", "512000", None)]),
    _azure("Storage", "Storage", "General Block Blob v2", "Hot LRS", "Hot Read Operations",
           [("0.004", "10K", "0", None)]),
    _azure("Storage", "Storage", "General Block Blob v2", "Hot LRS", "Hot LRS Write Operations",
           [("0.05", "10K", "0", None)]),
    _azure("Storage", "Storage", "General Block Blob v2", "Hot LRS", "All Other Operations",
           [("0.004", "10K", "0", None)]),
    _azure("Storage", "Storage", "General Block Blob v2", "Hot GRS", "Hot GRS Write Operations",
           [("0.1", "10K", "0", None)]),
]

GCP = [
    # Cloud Run functions (1st gen): invocations are global, CPU and memory regional.
    _gcp("Cloud Run Functions", "global", "ApplicationServices",
         "Cloud Run Functions (1st Gen) Invocations", "Functions",
         [("0", "count", "0", "2000000"), ("0.0000004", "count", "2000000", None)]),
    _gcp("Cloud Run Functions", "global", "ApplicationServices",
         "Cloud Run Functions Invocations", "Compute",
         [("0", "count", "0", "2000000"), ("0.0000004", "count", "2000000", None)]),
    _gcp("Cloud Run Functions", "us-central1", "ApplicationServices",
         "Cloud Run functions (1st Gen) CPU (Request-based billing)", "Functions",
         [("0.00001", "second", "0", None)]),
    _gcp("Cloud Run Functions", "us-central1", "ApplicationServices",
         "Cloud Run functions (1st Gen) Min Instance CPU (Request-based billing)", "Functions",
         [("0.000001042", "second", "0", None)]),
    _gcp("Cloud Run Functions", "us-central1", "ApplicationServices",
         "Cloud Run functions (1st Gen) Memory (Request-based billing)", "Functions",
         [("0.0000025", "gibibyte second", "0", None)]),
    _gcp("Cloud Run Functions", "us-central1", "ApplicationServices",
         "Cloud Run functions Memory (Request-based billing) in us-central1", "Compute",
         [("0.0000025", "gibibyte second", "0", None)]),
    _gcp("Cloud Run Functions", "southamerica-east1", "ApplicationServices",
         "Cloud Run functions (1st Gen) CPU Tier 2 (Request-based billing)", "Functions",
         [("0.000014", "second", "0", None)]),
    _gcp("Cloud Run Functions", "southamerica-east1", "ApplicationServices",
         "Cloud Run functions (1st Gen) Memory Tier 2 (Request-based billing)", "Functions",
         [("0.0000035", "gibibyte second", "0", None)]),
    # Cloud Run services, request-based billing.
    _gcp("Cloud Run", "global", "ApplicationServices", "Requests", "Compute",
         [("0", "count", "0", "2000000"), ("0.0000004", "count", "2000000", None)]),
    _gcp("Cloud Run", "us-central1", "ApplicationServices",
         "Services CPU (Request-based billing)", "Compute", [("0.000024", "second", "0", None)]),
    _gcp("Cloud Run", "us-central1", "ApplicationServices",
         "Services Min Instance CPU (Request-based billing)", "Compute",
         [("0.0000025", "second", "0", None)]),
    _gcp("Cloud Run", "us-central1", "ApplicationServices",
         "Services CPU (Instance-based billing) in us-central1", "Compute",
         [("0.000018", "second", "0", None)]),
    _gcp("Cloud Run", "us-central1", "ApplicationServices",
         "Services Memory (Request-based billing)", "Compute",
         [("0.0000025", "gibibyte second", "0", None)]),
    _gcp("Cloud Run", "us-central1", "ApplicationServices",
         "Services Min Instance Memory (Request-based billing)", "Compute",
         [("0.0000025", "gibibyte second", "0", None)]),
    _gcp("Cloud Run", "southamerica-east1", "ApplicationServices",
         "Services CPU Tier 2  (Request-based billing)", "Compute",
         [("0.0000336", "second", "0", None)]),
    _gcp("Cloud Run", "southamerica-east1", "ApplicationServices",
         "Services Memory Tier 2 (Request-based billing)", "Compute",
         [("0.0000035", "gibibyte second", "0", None)]),
    # Cloud Storage: regional Standard storage, and global operation prices.
    _gcp("Cloud Storage", "us-central1", "Storage", "Standard Storage US Regional",
         "RegionalStorage", [("0", "gibibyte month", "0", "5"),
                             ("0.02", "gibibyte month", "5", None)]),
    _gcp("Cloud Storage", "us-central1", "Storage", "Standard Storage Iowa Dual-region",
         "MultiRegionalStorage", [("0.022", "gibibyte month", "0", None)]),
    _gcp("Cloud Storage", "us-central1", "Storage", "Nearline Storage Iowa",
         "NearlineStorage", [("0.01", "gibibyte month", "0", None)]),
    _gcp("Cloud Storage", "global", "Storage", "Regional Standard Class A Operations",
         "RegionalOps", [("0", "count", "0", "5000"), ("0.000005", "count", "5000", None)]),
    _gcp("Cloud Storage", "global", "Storage", "Regional Standard Tagging Class A Operations",
         "RegionalOps", [("0", "count", "0", "5000"), ("0.000005", "count", "5000", None)]),
    _gcp("Cloud Storage", "global", "Storage", "Regional Standard Class B Operations",
         "RegionalOps", [("0", "count", "0", "50000"), ("0.0000004", "count", "50000", None)]),
    _gcp("Cloud Storage", "global", "Storage", "Dual-Region Standard Class A Operations",
         "StandardOps", [("0.00001", "count", "0", None)]),
    # Firestore (Standard edition): global catalogue, one product per location.
    _gcp("Cloud Firestore", "global", "ApplicationServices", "Cloud Firestore Read Ops Iowa",
         "FirestoreReadOps", [("0.0000003", "count", "0", None)]),
    _gcp("Cloud Firestore", "global", "ApplicationServices",
         "Cloud Firestore Read Ops (with free tier) Iowa", "FirestoreReadOps",
         [("0", "count", "0", "50000"), ("0.0000003", "count", "50000", None)]),
    _gcp("Cloud Firestore", "global", "ApplicationServices", "Cloud Firestore Read Ops Belgium",
         "FirestoreReadOps", [("0.00000033", "count", "0", None)]),
    _gcp("Cloud Firestore", "global", "ApplicationServices", "Cloud Firestore Entity Writes Iowa",
         "FirestoreEntityPutOps", [("0.0000009", "count", "0", None)]),
    _gcp("Cloud Firestore", "global", "ApplicationServices", "Cloud Firestore Storage Iowa",
         "FirestoreStorage", [("0.15", "gibibyte month", "0", None)]),
    _gcp("Cloud Firestore", "global", "ApplicationServices",
         "Cloud Firestore Point-in-time Recovery Storage Iowa", "FirestorePITRStorage",
         [("0.15", "gibibyte month", "0", None)]),
]

CATALOGUE = AZURE_EASTUS + GCP


def _fake_post(catalogue=CATALOGUE, captured=None):
    """Return a `requests.post` stand-in that filters *catalogue* like the API."""
    def post(url, headers=None, json=None, timeout=None):
        variables = json["variables"]
        if captured is not None:
            captured.append(variables)
        family = variables.get("productFamily")
        filters = variables.get("attributeFilters") or []
        matched = []
        for product in catalogue:
            if (product["vendor"], product["service"], product["region"]) != (
                    variables["vendorName"], variables["service"], variables["region"]):
                continue
            attrs = {a["key"]: a["value"] for a in product["attributes"]}
            if family and product["productFamily"] != family:
                continue
            if all(attrs.get(f["key"]) == f["value"] for f in filters):
                matched.append(product)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"data": {"products": matched}}
        resp.raise_for_status.return_value = None
        return resp
    return post


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("INFRACOST_API_KEY", "test-token")
    monkeypatch.setenv("INFRACOST_ORG_ID", "org-123")


def _sync(metric, region, catalogue=CATALOGUE):
    upserted = []
    cache = MagicMock()
    cache.upsert.side_effect = upserted.append
    with patch.object(ic.requests, "post", side_effect=_fake_post(catalogue)):
        n = ic.InfracostClient().sync_to_cache(cache, metric, region)
    assert n == len(upserted)
    return upserted


def _tiers(rows):
    return sorted((r.start_usage_amount, r.end_usage_amount, r.price_usd) for r in rows)


# --- One product per metric, stored under the handler's service ---------------

# metric, region, vendor, store service, unit, [(start, end, price per unit)]
EXPECTED = [
    ("AzureFunctions-Execution", "eastus", "azure", "AzureFunctions", "Executions",
     [(0, 1_000_000, 0), (1_000_000, None, 0.0000002)]),
    ("AzureFunctions-GB-Second", "eastus", "azure", "AzureFunctions", "GB-Seconds",
     [(0, 400_000, 0), (400_000, None, 0.000016)]),
    ("CosmosDB-Serverless-RU", "eastus", "azure", "CosmosDB", "RUs",
     [(0, None, 0.00000025)]),
    ("CosmosDB-Storage-GB-Month", "eastus", "azure", "CosmosDB", "GB-Mo",
     [(0, None, 0.25)]),
    ("APIM-Consumption-Call", "eastus", "azure", "APIManagement", "Calls",
     [(0, 1_000_000, 0), (1_000_000, None, 0.0000035)]),
    ("Blob-Hot-LRS-GB-Month", "eastus", "azure", "BlobStorage", "GB-Mo",
     [(0, 51_200, 0.0208), (51_200, 512_000, 0.019968), (512_000, None, 0.019136)]),
    ("Blob-Hot-Read-Operation", "eastus", "azure", "BlobStorage", "Requests",
     [(0, None, 0.0000004)]),
    ("Blob-Hot-LRS-Write-Operation", "eastus", "azure", "BlobStorage", "Requests",
     [(0, None, 0.000005)]),
    ("CloudFunctions-Invocation", "us-central1", "gcp", "CloudFunctions", "count",
     [(0, 2_000_000, 0), (2_000_000, None, 0.0000004)]),
    ("CloudFunctions-GHz-Second", "us-central1", "gcp", "CloudFunctions", "second",
     [(0, None, 0.00001)]),
    ("CloudFunctions-GB-Second", "us-central1", "gcp", "CloudFunctions", "gibibyte second",
     [(0, None, 0.0000025)]),
    ("CloudFunctions-GHz-Second", "southamerica-east1", "gcp", "CloudFunctions", "second",
     [(0, None, 0.000014)]),
    ("CloudRun-Request", "us-central1", "gcp", "CloudRun", "count",
     [(0, 2_000_000, 0), (2_000_000, None, 0.0000004)]),
    ("CloudRun-vCPU-Second", "us-central1", "gcp", "CloudRun", "second",
     [(0, None, 0.000024)]),
    ("CloudRun-GiB-Second", "us-central1", "gcp", "CloudRun", "gibibyte second",
     [(0, None, 0.0000025)]),
    ("CloudRun-vCPU-Second", "southamerica-east1", "gcp", "CloudRun", "second",
     [(0, None, 0.0000336)]),
    ("CloudRun-GiB-Second", "southamerica-east1", "gcp", "CloudRun", "gibibyte second",
     [(0, None, 0.0000035)]),
    ("GCS-Standard-GiB-Month", "us-central1", "gcp", "CloudStorage", "gibibyte month",
     [(0, 5, 0), (5, None, 0.02)]),
    ("GCS-Class-A-Operation", "us-central1", "gcp", "CloudStorage", "count",
     [(0, 5_000, 0), (5_000, None, 0.000005)]),
    ("GCS-Class-B-Operation", "us-central1", "gcp", "CloudStorage", "count",
     [(0, 50_000, 0), (50_000, None, 0.0000004)]),
    ("Firestore-Read", "us-central1", "gcp", "Firestore", "count",
     [(0, None, 0.0000003)]),
    ("Firestore-Read", "europe-west1", "gcp", "Firestore", "count",
     [(0, None, 0.00000033)]),
    ("Firestore-Write", "us-central1", "gcp", "Firestore", "count",
     [(0, None, 0.0000009)]),
    ("Firestore-GiB-Month", "us-central1", "gcp", "Firestore", "gibibyte month",
     [(0, None, 0.15)]),
]


@pytest.mark.parametrize("metric,region,vendor,service,unit,tiers", EXPECTED,
                         ids=[f"{e[0]}@{e[1]}" for e in EXPECTED])
def test_descriptor_stores_one_product(creds, metric, region, vendor, service, unit, tiers):
    rows = _sync(metric, region)
    assert rows, f"{metric} stored no rows in {region}"
    products = {tuple(sorted(r.attributes.items())) for r in rows}
    assert len(products) == 1
    assert {(r.vendor, r.service, r.region, r.usage_metric, r.unit) for r in rows} == {
        (vendor, service, region, metric, unit)}
    assert all(r.source == "infracost" for r in rows)
    got = _tiers(rows)
    assert [(s, e) for s, e, _ in got] == [(s, e) for s, e, _ in tiers]
    assert [p for _, _, p in got] == pytest.approx([p for _, _, p in tiers])


def test_every_azure_and_gcp_descriptor_is_covered():
    covered = {e[0] for e in EXPECTED}
    new = {m for m, d in ic.METRIC_DESCRIPTORS.items() if d.get("vendor") in ("azure", "gcp")}
    assert new == covered


# --- Region handling ------------------------------------------------------------


@pytest.mark.parametrize("region,location", [
    ("us-central1", "Iowa"), ("europe-west1", "Belgium"), ("us-east4", "Northern Virginia"),
    ("asia-northeast1", "Tokyo"),
])
def test_firestore_descriptor_resolves_gcp_location(creds, region, location):
    """Firestore rows live in the global catalogue and name the location in the
    description, so the sync region maps to that name, as REGION_PREFIX does for AWS."""
    captured = []
    with patch.object(ic.requests, "post", side_effect=_fake_post([], captured)):
        ic.InfracostClient().sync_to_cache(MagicMock(), "Firestore-Read", region)
    assert captured[0]["region"] == "global"
    assert captured[0]["attributeFilters"] == [
        {"key": "description", "value": f"Cloud Firestore Read Ops {location}"}]


def test_unknown_gcp_region_stores_nothing(creds):
    assert _sync("Firestore-Read", "mars-north1") == []


@pytest.mark.parametrize("vendor,expected", [
    ("aws", sorted(ic._REGION_PREFIX)),
    ("azure", sorted(ic._AZURE_REGIONS)),
    ("gcp", sorted(ic._GCP_LOCATION)),
])
def test_sync_regions_per_vendor(vendor, expected):
    assert ic.sync_regions(vendor) == expected
    assert "eastus" in ic.sync_regions("azure")
    assert "us-central1" in ic.sync_regions("gcp")


# --- Vendor dispatch in sync_pricing_catalog -----------------------------------


def _vendors_synced(monkeypatch, vendor):
    calls = []

    def fake_sync(self, cache, metric, region, vendor="aws"):
        calls.append(ic.METRIC_DESCRIPTORS[metric].get("vendor", "aws"))
        return 1

    monkeypatch.setattr(ic.InfracostClient, "sync_to_cache", fake_sync)
    monkeypatch.setattr("infra_cost_model.pricing.cache.PricingCache",
                        lambda *a, **k: MagicMock())
    ic.sync_pricing_catalog(vendor=vendor, regions=["r"])
    return set(calls)


@pytest.mark.parametrize("vendor", ["aws", "azure", "gcp"])
def test_sync_pricing_catalog_syncs_only_the_vendor_descriptors(creds, monkeypatch, vendor):
    assert _vendors_synced(monkeypatch, vendor) == {vendor}


# --- The handlers reach the synced rows ----------------------------------------


HANDLERS = [AzureFunction, CosmosDB, APIManagement, AzureBlobStorage,
            CloudFunction, CloudStorage, CloudRun, Firestore]

_SERVICE = {
    AzureFunction: "AzureFunctions", CosmosDB: "CosmosDB", APIManagement: "APIManagement",
    AzureBlobStorage: "BlobStorage", CloudFunction: "CloudFunctions",
    CloudStorage: "CloudStorage", CloudRun: "CloudRun", Firestore: "Firestore",
}


def _handler_catalog_metrics(handler):
    names = set(handler().catalog_metrics.values())
    usage = {m: 1.0 for m in handler().valid_metrics}
    derived = handler().derive_catalog_usage(usage)
    if derived is not None:
        names |= set(derived.quantities)
    return names


@pytest.mark.parametrize("handler", HANDLERS, ids=lambda h: h.__name__)
def test_handler_catalog_metrics_have_descriptors(handler):
    names = _handler_catalog_metrics(handler)
    assert names, f"{handler.__name__} maps no logical metric to the catalog"
    for name in names:
        d = ic.METRIC_DESCRIPTORS[name]
        assert d["store_service"] == _SERVICE[handler]
        assert d["vendor"] == ("azure" if handler.__module__.endswith("azure") else "gcp")


def test_azure_function_bills_rounded_memory_and_minimum_duration():
    derived = AzureFunction().derive_catalog_usage(
        {"invocations": 10.0, "avgDurationMs": 50.0, "memoryMb": 200.0})
    # 256 MB (rounded up to 128 MB steps) for 100 ms (the minimum), 10 times.
    assert derived.quantities == {"AzureFunctions-Execution": 10.0,
                                  "AzureFunctions-GB-Second": pytest.approx(0.25)}


@pytest.mark.parametrize("memory_mb,gb,ghz", [
    (128, 0.125, 0.2), (256, 0.25, 0.4), (512, 0.5, 0.8), (1024, 1.0, 1.4),
    (2048, 2.0, 2.4), (4096, 4.0, 4.8), (8192, 8.0, 4.8), (300, 0.5, 0.8),
])
def test_cloud_function_derives_gb_and_ghz_seconds(memory_mb, gb, ghz):
    derived = CloudFunction().derive_catalog_usage(
        {"invocations": 1.0, "avgDurationMs": 1000.0, "memoryMb": float(memory_mb)})
    assert derived.quantities == {
        "CloudFunctions-Invocation": 1.0,
        "CloudFunctions-GB-Second": pytest.approx(gb),
        "CloudFunctions-GHz-Second": pytest.approx(ghz),
    }


def test_cloud_function_rounds_duration_up_to_100_ms():
    derived = CloudFunction().derive_catalog_usage(
        {"invocations": 1.0, "avgDurationMs": 101.0, "memoryMb": 1024.0})
    assert derived.quantities["CloudFunctions-GB-Second"] == pytest.approx(0.2)


def _synced_catalog(tmp_path, metrics_by_region):
    cache = PricingCache(db_path=tmp_path / "pricing.db")
    client = ic.InfracostClient()
    with patch.object(ic.requests, "post", side_effect=_fake_post()):
        for region, metrics in metrics_by_region.items():
            for metric in metrics:
                client.sync_to_cache(cache, metric, region)
    return PricingCatalog(db_path=tmp_path / "pricing.db")


def _node(address, provider, service, region, metrics):
    return {
        "nodeType": "compute", "resourceAddress": address, "provider": provider,
        "service": service, "region": region,
        "usageMetrics": {k: {"unit": "x", "value": v} for k, v in metrics.items()},
    }


def _monthly(catalog, node, invocations_per_month):
    model = {
        "version": "1.0",
        "workflow": {"name": "w", "entry": "n",
                     "frequency": {"unit": "perMonth", "value": invocations_per_month}},
        "nodes": {"n": node},
        "edges": [],
    }
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        costs = CostEngine(model, catalog=catalog, time_basis="monthly").compute()
    unpriced = [w.message.unpriced.metric for w in caught
                if isinstance(w.message, UnpricedMetricWarning)]
    return costs["n"], unpriced


def test_engine_prices_an_azure_function_from_synced_rows(creds, tmp_path):
    catalog = _synced_catalog(tmp_path, {"eastus": [
        "AzureFunctions-Execution", "AzureFunctions-GB-Second"]})
    node = _node("azurerm_linux_function_app.fn", "azure", "AzureFunctions", "eastus",
                 {"invocations": 1, "avgDurationMs": 1000, "memoryMb": 512})
    cost, unpriced = _monthly(catalog, node, 3_000_000)
    # 2M executions above the free 1M at $0.20 a million, and
    # 1.5M GB-s minus the free 400,000 at $0.000016.
    assert cost == pytest.approx(0.40 + 1_100_000 * 0.000016, rel=1e-6)
    assert unpriced == []


def test_engine_prices_a_cloud_run_service_from_synced_rows(creds, tmp_path):
    catalog = _synced_catalog(tmp_path, {"us-central1": [
        "CloudRun-Request", "CloudRun-vCPU-Second", "CloudRun-GiB-Second"]})
    node = _node("google_cloud_run_service.api", "gcp", "CloudRun", "us-central1",
                 {"requests": 1, "vcpuSeconds": 0.5, "memoryGbSeconds": 0.25})
    node["nodeType"] = "routing"
    cost, unpriced = _monthly(catalog, node, 4_000_000)
    # 2M requests above the free 2M at $0.40 a million, 2M vCPU-s at
    # $0.000024 and 1M GiB-s at $0.0000025.
    assert cost == pytest.approx(0.80 + 2_000_000 * 0.000024 + 1_000_000 * 0.0000025,
                                 rel=1e-6)
    assert unpriced == []


def test_engine_prices_firestore_from_synced_rows(creds, tmp_path):
    catalog = _synced_catalog(tmp_path, {"us-central1": [
        "Firestore-Read", "Firestore-Write"]})
    node = _node("google_firestore_database.db", "gcp", "Firestore", "us-central1",
                 {"readRequests": 2, "writeRequests": 1})
    node["nodeType"] = "storage"
    cost, unpriced = _monthly(catalog, node, 1_000_000)
    assert cost == pytest.approx(2_000_000 * 0.0000003 + 1_000_000 * 0.0000009, rel=1e-6)
    assert unpriced == []
