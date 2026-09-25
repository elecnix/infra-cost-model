"""Infracost descriptors for the Azure and GCP handlers (#226).

The fake Cloud Pricing API below holds a slice of the products that the live
Infracost API returned on 2026-09-24 for Azure (eastus) and GCP (us-central1,
southamerica-east1, europe-west1, africa-south1 and the global catalogue). It
answers a query the way the live API does: it keeps the products of the
vendor, service and region whose product family and attributes equal the
filter values.

The fake Azure Retail Prices API holds the items that the public API
returned on 2026-09-24 for the meters that Infracost lacks or gives with
stale tiers (#372, #376).
"""

import re
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
    # Infracost gives the egress meter with two sets of tier starts: the
    # current one, which starts paying at 100 GB, and an older one at 5 GB
    # (#372). The descriptor reads the Azure Retail Prices API instead.
    _azure("Bandwidth", "Networking", "Rtn Preference: MGN", "Standard",
           "Standard Data Transfer Out", [
               ("0", "1 GB", "0", None), ("0.087", "1 GB", "5", None),
               ("0.087", "1 GB", "100", None), ("0.083", "1 GB", "10240", None),
               ("0.083", "1 GB", "10335", None), ("0.07", "1 GB", "51200", None),
               ("0.07", "1 GB", "51295", None), ("0.05", "1 GB", "153600", None),
               ("0.05", "1 GB", "153695", None), ("0.05", "1 GB", "512000", None),
               ("0.05", "1 GB", "512095", None)]),
]

# In canadacentral, eastus2, southeastasia, southindia and switzerlandnorth,
# Infracost has the other Blob meters but no "Hot LRS Write Operations" (#376).
AZURE_EASTUS2 = [
    _azure("Storage", "Storage", "General Block Blob v2", "Hot LRS", "Hot Read Operations",
           [("0.004", "10K", "0", None)], region="eastus2"),
]


def _retail(region, service, family, product_name, sku, meter, unit, tiers,
            effective="2016-05-09T00:00:00Z"):
    meter_id = f"{product_name}/{sku}/{meter}"
    return [{
        "currencyCode": "USD", "retailPrice": price, "unitPrice": price,
        "armRegionName": region, "location": region, "effectiveStartDate": effective,
        "meterId": meter_id, "meterName": meter, "productId": "P", "skuId": "S",
        "productName": product_name, "skuName": sku, "serviceName": service,
        "serviceId": "SV", "serviceFamily": family, "unitOfMeasure": unit,
        "type": "Consumption", "isPrimaryMeterRegion": True, "armSkuName": "",
        "tierMinimumUnits": start,
    } for start, price in tiers]


RETAIL = (
    _retail("eastus2", "Storage", "Storage", "General Block Blob v2", "Hot LRS",
            "Hot LRS Write Operations", "10K", [(0.0, 0.05)])
    + _retail("canadacentral", "Storage", "Storage", "General Block Blob v2", "Hot LRS",
              "Hot LRS Write Operations", "10K", [(0.0, 0.055)],
              effective="2020-01-01T00:00:00Z")
    + _retail("eastus", "Bandwidth", "Networking", "Rtn Preference: MGN", "Standard",
              "Standard Data Transfer Out", "1 GB", [
                  (0.0, 0.0), (100.0, 0.087), (10335.0, 0.083), (51295.0, 0.07),
                  (153695.0, 0.05), (512095.0, 0.05)],
              effective="2022-05-01T00:00:00Z")
    + _retail("southeastasia", "Bandwidth", "Networking", "Rtn Preference: MGN",
              "Standard", "Standard Data Transfer Out", "1 GB", [
                  (0.0, 0.0), (100.0, 0.12), (10335.0, 0.085), (51295.0, 0.082),
                  (153695.0, 0.08), (512095.0, 0.08)],
              effective="2022-05-01T00:00:00Z")
)

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
    # africa-south1 is a Tier 1 region, but the API gives it both tiers (#376).
    _gcp("Cloud Run", "africa-south1", "ApplicationServices",
         "Services CPU (Request-based billing)", "Compute", [("0.000024", "second", "0", None)]),
    _gcp("Cloud Run", "africa-south1", "ApplicationServices",
         "Services Memory (Request-based billing)", "Compute",
         [("0.0000025", "gibibyte second", "0", None)]),
    _gcp("Cloud Run", "africa-south1", "ApplicationServices",
         "Services CPU Tier 2  (Request-based billing)", "Compute",
         [("0.0000336", "second", "0", None)]),
    _gcp("Cloud Run", "africa-south1", "ApplicationServices",
         "Services Memory Tier 2 (Request-based billing)", "Compute",
         [("0.0000035", "gibibyte second", "0", None)]),
    # Cloud Run internet egress (#372): each region has one product for
    # traffic to its own continent. North America includes 1 GiB free.
    _gcp("Cloud Run", "us-central1", "Network",
         "Cloud Run Network Internet Data Transfer Out North America to North America",
         "PremiumInternetEgress", [
             ("0", "gibibyte", "0", "1"), ("0.105", "gibibyte", "1", "10240"),
             ("0.08", "gibibyte", "10240", "153600"), ("0.06", "gibibyte", "153600", None)]),
    _gcp("Cloud Run", "us-central1", "Network",
         "Cloud Run Network Inter Region Data Transfer Out North America to North America",
         "InterregionEgress", [("0", "gibibyte", "0", "1"), ("0.01", "gibibyte", "1", None)]),
    _gcp("Cloud Run", "europe-west1", "Network",
         "Cloud Run Network Internet Data Transfer Out Europe to Europe",
         "PremiumInternetEgress", [
             ("0.105", "gibibyte", "0", "10240"), ("0.08", "gibibyte", "10240", "153600"),
             ("0.06", "gibibyte", "153600", None)]),
    _gcp("Cloud Run", "me-west1", "Network",
         "Cloud Run Network Internet Data Transfer Out MiddleEast to MiddleEast",
         "PremiumInternetEgress", [
             ("0.14", "gibibyte", "0", "10240"), ("0.11", "gibibyte", "10240", "153600"),
             ("0.09", "gibibyte", "153600", None)]),
    _gcp("Cloud Run", "me-west1", "Network",
         "Cloud Run Network Internet Egress AsiaPacific to AsiaPacific",
         "PremiumInternetEgress", [
             ("0.12", "gibibyte", "0", "10240"), ("0.085", "gibibyte", "10240", "153600"),
             ("0.08", "gibibyte", "153600", None)]),
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
    # Cloud Storage internet egress (#372), in the global catalogue.
    _gcp("Cloud Storage", "global", "Network",
         "Download Worldwide Destinations (excluding Asia & Australia)",
         "PremiumInternetEgress", [
             ("0", "gibibyte", "0", "100"), ("0.12", "gibibyte", "100", "1024"),
             ("0.11", "gibibyte", "1024", "10240"), ("0.08", "gibibyte", "10240", None)]),
    _gcp("Cloud Storage", "global", "Network", "Download APAC", "PremiumInternetEgress", [
        ("0", "gibibyte", "0", "100"), ("0.12", "gibibyte", "100", "1024"),
        ("0.11", "gibibyte", "1024", "10240"), ("0.08", "gibibyte", "10240", None)]),
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

CATALOGUE = AZURE_EASTUS + AZURE_EASTUS2 + GCP


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


_CLAUSE = re.compile(r"(\w+) eq '((?:[^']|'')*)'")


def _fake_get(items=RETAIL, captured=None):
    """Return a `requests.get` stand-in for the Azure Retail Prices API.

    It keeps the items whose fields equal every `eq` clause of the filter,
    and gives them in two pages, as the live API does for long answers.
    """
    def get(url, params=None, timeout=None):
        if captured is not None:
            captured.append((url, params))
        if params is None:  # the NextPageLink of the first page
            page = items_by_link.pop(url)
            link = None
        else:
            clauses = {k: v.replace("''", "'") for k, v in _CLAUSE.findall(params["$filter"])}
            if "priceType" in clauses:
                clauses["type"] = clauses.pop("priceType")
            matched = [i for i in items
                       if all(str(i.get(k)) == v for k, v in clauses.items())]
            half = (len(matched) + 1) // 2
            page, rest = matched[:half], matched[half:]
            link = None
            if rest:
                link = f"https://prices.azure.com/api/retail/prices?page={len(items_by_link)}"
                items_by_link[link] = rest
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"Items": page, "NextPageLink": link}
        resp.raise_for_status.return_value = None
        return resp
    items_by_link = {}
    return get


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("INFRACOST_API_KEY", "test-token")
    monkeypatch.setenv("INFRACOST_ORG_ID", "org-123")


def _sync(metric, region, catalogue=CATALOGUE, retail=RETAIL):
    upserted = []
    cache = MagicMock()
    cache.upsert.side_effect = upserted.append
    with patch.object(ic.requests, "post", side_effect=_fake_post(catalogue)), \
            patch.object(ic.requests, "get", side_effect=_fake_get(retail)):
        n = ic.InfracostClient().sync_to_cache(cache, metric, region)
    assert n == len(upserted)
    return upserted


def _tiers(rows):
    return sorted((r.start_usage_amount, r.end_usage_amount, r.price_usd) for r in rows)


def _bounds(tiers):
    """The tier bounds, flat, with -1 for an open end, for `pytest.approx`."""
    return [-1 if b is None else b for s, e, _ in tiers for b in (s, e)]


# --- One product per metric, stored under the handler's service ---------------

# The monthly free allowances of Firestore (#373): GCP gives 50,000 reads and
# 20,000 writes a day, and a month has 365.25 / 12 = 30.4375 days on average.
FIRESTORE_FREE_READS = 50_000 * 30.4375
FIRESTORE_FREE_WRITES = 20_000 * 30.4375
# GCP gives the Cloud Run free tier as a discount at Tier 1 prices, so a
# Tier 2 region gets fewer free units (#373).
TIER_2_FREE_VCPU_SECONDS = 180_000 * 0.000024 / 0.0000336
TIER_2_FREE_GIB_SECONDS = 360_000 * 0.0000025 / 0.0000035

# metric, region, vendor, store service, unit, [(start, end, price per unit)]
EXPECTED = [
    ("AzureFunctions-Execution", "eastus", "azure", "AzureFunctions", "executions",
     [(0, 1_000_000, 0), (1_000_000, None, 0.0000002)]),
    ("AzureFunctions-GB-Second", "eastus", "azure", "AzureFunctions", "GB-s",
     [(0, 400_000, 0), (400_000, None, 0.000016)]),
    ("CosmosDB-Serverless-RU", "eastus", "azure", "CosmosDB", "RUs",
     [(0, None, 0.00000025)]),
    ("CosmosDB-Storage-GB-Month", "eastus", "azure", "CosmosDB", "GB-Mo",
     [(0, None, 0.25)]),
    ("APIM-Consumption-Call", "eastus", "azure", "APIManagement", "requests",
     [(0, 1_000_000, 0), (1_000_000, None, 0.0000035)]),
    ("Blob-Hot-LRS-GB-Month", "eastus", "azure", "BlobStorage", "GB-Mo",
     [(0, 51_200, 0.0208), (51_200, 512_000, 0.019968), (512_000, None, 0.019136)]),
    ("Blob-Hot-Read-Operation", "eastus", "azure", "BlobStorage", "requests",
     [(0, None, 0.0000004)]),
    ("Blob-Hot-LRS-Write-Operation", "eastus", "azure", "BlobStorage", "requests",
     [(0, None, 0.000005)]),
    ("Bandwidth-Internet-Out-GB", "eastus", "azure", "Bandwidth", "GB",
     [(0, 100, 0), (100, 10_335, 0.087), (10_335, 51_295, 0.083),
      (51_295, 153_695, 0.07), (153_695, 512_095, 0.05), (512_095, None, 0.05)]),
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
     [(0, 180_000, 0), (180_000, None, 0.000024)]),
    ("CloudRun-GiB-Second", "us-central1", "gcp", "CloudRun", "gibibyte second",
     [(0, 360_000, 0), (360_000, None, 0.0000025)]),
    ("CloudRun-vCPU-Second", "southamerica-east1", "gcp", "CloudRun", "second",
     [(0, TIER_2_FREE_VCPU_SECONDS, 0), (TIER_2_FREE_VCPU_SECONDS, None, 0.0000336)]),
    ("CloudRun-GiB-Second", "southamerica-east1", "gcp", "CloudRun", "gibibyte second",
     [(0, TIER_2_FREE_GIB_SECONDS, 0), (TIER_2_FREE_GIB_SECONDS, None, 0.0000035)]),
    ("CloudRun-vCPU-Second", "africa-south1", "gcp", "CloudRun", "second",
     [(0, 180_000, 0), (180_000, None, 0.000024)]),
    ("CloudRun-GiB-Second", "africa-south1", "gcp", "CloudRun", "gibibyte second",
     [(0, 360_000, 0), (360_000, None, 0.0000025)]),
    ("CloudRun-Internet-Egress-GiB", "us-central1", "gcp", "CloudRun", "gibibyte",
     [(0, 1, 0), (1, 10_240, 0.105), (10_240, 153_600, 0.08), (153_600, None, 0.06)]),
    ("CloudRun-Internet-Egress-GiB", "europe-west1", "gcp", "CloudRun", "gibibyte",
     [(0, 10_240, 0.105), (10_240, 153_600, 0.08), (153_600, None, 0.06)]),
    ("CloudRun-Internet-Egress-GiB", "me-west1", "gcp", "CloudRun", "gibibyte",
     [(0, 10_240, 0.14), (10_240, 153_600, 0.11), (153_600, None, 0.09)]),
    ("GCS-Standard-GiB-Month", "us-central1", "gcp", "CloudStorage", "gibibyte month",
     [(0, 5, 0), (5, None, 0.02)]),
    ("GCS-Class-A-Operation", "us-central1", "gcp", "CloudStorage", "count",
     [(0, 5_000, 0), (5_000, None, 0.000005)]),
    ("GCS-Class-B-Operation", "us-central1", "gcp", "CloudStorage", "count",
     [(0, 50_000, 0), (50_000, None, 0.0000004)]),
    ("GCS-Internet-Egress-GiB", "us-central1", "gcp", "CloudStorage", "gibibyte",
     [(0, 100, 0), (100, 1_024, 0.12), (1_024, 10_240, 0.11), (10_240, None, 0.08)]),
    # The 100 GiB of Always Free egress applies in three US regions only.
    ("GCS-Internet-Egress-GiB", "europe-west1", "gcp", "CloudStorage", "gibibyte",
     [(0, 1_024, 0.12), (1_024, 10_240, 0.11), (10_240, None, 0.08)]),
    ("Firestore-Read", "us-central1", "gcp", "Firestore", "count",
     [(0, FIRESTORE_FREE_READS, 0), (FIRESTORE_FREE_READS, None, 0.0000003)]),
    ("Firestore-Read", "europe-west1", "gcp", "Firestore", "count",
     [(0, FIRESTORE_FREE_READS, 0), (FIRESTORE_FREE_READS, None, 0.00000033)]),
    ("Firestore-Write", "us-central1", "gcp", "Firestore", "count",
     [(0, FIRESTORE_FREE_WRITES, 0), (FIRESTORE_FREE_WRITES, None, 0.0000009)]),
    ("Firestore-GiB-Month", "us-central1", "gcp", "Firestore", "gibibyte month",
     [(0, 1, 0), (1, None, 0.15)]),
]

# The descriptors that read the Azure Retail Prices API (#372).
RETAIL_SOURCED = {"Bandwidth-Internet-Out-GB"}


@pytest.mark.parametrize("metric,region,vendor,service,unit,tiers", EXPECTED,
                         ids=[f"{e[0]}@{e[1]}" for e in EXPECTED])
def test_descriptor_stores_one_product(creds, metric, region, vendor, service, unit, tiers):
    rows = _sync(metric, region)
    assert rows, f"{metric} stored no rows in {region}"
    products = {tuple(sorted(r.attributes.items())) for r in rows}
    assert len(products) == 1
    assert {(r.vendor, r.service, r.region, r.usage_metric, r.unit) for r in rows} == {
        (vendor, service, region, metric, unit)}
    source = "azure-retail" if metric in RETAIL_SOURCED else "infracost"
    assert all(r.source == source for r in rows)
    got = _tiers(rows)
    assert _bounds(got) == pytest.approx(_bounds(tiers))
    assert [p for _, _, p in got] == pytest.approx([p for _, _, p in tiers])


def test_every_scaled_descriptor_names_its_stored_unit():
    """A row priced per unit must not keep the API's block unit ("10K")."""
    scaled = {m: d for m, d in ic.METRIC_DESCRIPTORS.items() if "unit_scale" in d}
    assert scaled
    assert [m for m, d in scaled.items() if "store_unit" not in d] == []


def test_every_azure_and_gcp_descriptor_is_covered():
    covered = {e[0] for e in EXPECTED}
    # test_azure_openai_models.py covers the Azure OpenAI descriptors (#371).
    new = {m for m, d in ic.METRIC_DESCRIPTORS.items() if d.get("vendor") in ("azure", "gcp")
           and d.get("store_service") != "AzureOpenAI"}
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


def test_close_open_tiers_leaves_a_tier_open_when_the_next_one_starts_with_it():
    """Two rows with one start can't bound each other: an end at the start
    would make an empty tier."""
    rows = [{"start_usage_amount": 0.0, "end_usage_amount": None, "price_usd": 1.0},
            {"start_usage_amount": 0.0, "end_usage_amount": None, "price_usd": 2.0},
            {"start_usage_amount": 10.0, "end_usage_amount": None, "price_usd": 3.0}]
    closed = ic._close_open_tiers(rows)
    assert [(r["start_usage_amount"], r["end_usage_amount"]) for r in closed] == [
        (0.0, 10.0), (0.0, 10.0), (10.0, None)]


def test_unknown_gcp_region_stores_nothing(creds):
    assert _sync("Firestore-Read", "mars-north1") == []


@pytest.mark.parametrize("vendor,expected", [
    ("aws", sorted(ic._REGION_PREFIX) + [ic.GLOBAL_REGION]),
    ("azure", sorted(ic._AZURE_REGIONS)),
    ("gcp", sorted(ic._GCP_LOCATION)),
])
def test_sync_regions_per_vendor(vendor, expected):
    assert ic.sync_regions(vendor) == expected


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
        # A handler names another service when the provider bills the
        # metric under it, as Azure bills egress as Bandwidth (#372).
        service = handler().catalog_services.get(name, _SERVICE[handler])
        assert d["store_service"] == service
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


@pytest.mark.parametrize("duration_ms,seconds", [(101.0, 0.2), (0.0, 0.1), (1.0, 0.1)])
def test_cloud_function_rounds_duration_up_to_100_ms(duration_ms, seconds):
    derived = CloudFunction().derive_catalog_usage(
        {"invocations": 1.0, "avgDurationMs": duration_ms, "memoryMb": 1024.0})
    assert derived.quantities["CloudFunctions-GB-Second"] == pytest.approx(seconds)


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
    # 2M requests above the free 2M at $0.40 a million, 2M vCPU-s above
    # the free 180,000 at $0.000024, and 1M GiB-s above the free 360,000 at
    # $0.0000025 (#373).
    assert cost == pytest.approx(0.80 + 1_820_000 * 0.000024 + 640_000 * 0.0000025,
                                 rel=1e-6)
    assert unpriced == []


def test_engine_prices_firestore_from_synced_rows(creds, tmp_path):
    catalog = _synced_catalog(tmp_path, {"us-central1": [
        "Firestore-Read", "Firestore-Write"]})
    node = _node("google_firestore_database.db", "gcp", "Firestore", "us-central1",
                 {"readRequests": 2, "writeRequests": 1})
    node["nodeType"] = "storage"
    cost, unpriced = _monthly(catalog, node, 1_000_000)
    # The reads and writes above a month of the daily free quota (#373).
    assert cost == pytest.approx((2_000_000 - FIRESTORE_FREE_READS) * 0.0000003
                                 + (1_000_000 - FIRESTORE_FREE_WRITES) * 0.0000009,
                                 rel=1e-6)
    assert unpriced == []


# --- The synced Azure rows match the seed rows (#363) ---------------------------


def _seed_rows(service, metric):
    from infra_cost_model.pricing.cache import load_seed_rows

    return [r for r in load_seed_rows([service])
            if (r.vendor, r.region, r.usage_metric) == ("azure", "eastus", metric)]


AZURE_SEEDED = [(e[0], e[3]) for e in EXPECTED if e[2] == "azure"]


@pytest.mark.parametrize("metric,service", AZURE_SEEDED, ids=[m for m, _ in AZURE_SEEDED])
def test_synced_azure_rows_match_the_seed_rows(creds, metric, service):
    """A live sync replaces the seed rows, so it must store the same units and tiers."""
    seed = _seed_rows(service, metric)
    assert seed, f"no eastus seed row for {metric}"
    synced = _sync(metric, "eastus")
    assert {r.unit for r in synced} == {r.unit for r in seed}
    got, want = _tiers(synced), _tiers(seed)
    assert [(s or 0, e) for s, e, _ in got] == [(s or 0, e) for s, e, _ in want]
    assert [p for _, _, p in got] == pytest.approx([p for _, _, p in want])


# --- Egress: every handler with dataOutGb reaches a catalog metric (#372) ------


@pytest.mark.parametrize("handler,metric,service", [
    (APIManagement, "Bandwidth-Internet-Out-GB", "Bandwidth"),
    (AzureBlobStorage, "Bandwidth-Internet-Out-GB", "Bandwidth"),
    (CloudStorage, "GCS-Internet-Egress-GiB", None),
    (CloudRun, "CloudRun-Internet-Egress-GiB", None),
], ids=lambda v: getattr(v, "__name__", v))
def test_data_out_maps_to_an_egress_metric(handler, metric, service):
    assert handler().catalog_metrics["dataOutGb"] == metric
    assert handler().catalog_services.get(metric) == service


def test_azure_egress_nodes_share_the_bandwidth_tiers(creds, tmp_path):
    """Azure bills a storage account's and an API gateway's egress on one
    Bandwidth meter, so the 100 GB a month free covers both (#372)."""
    cache = PricingCache(db_path=tmp_path / "pricing.db")
    with patch.object(ic.requests, "post", side_effect=_fake_post()), \
            patch.object(ic.requests, "get", side_effect=_fake_get()):
        ic.InfracostClient().sync_to_cache(cache, "Bandwidth-Internet-Out-GB", "eastus")
    catalog = PricingCatalog(db_path=tmp_path / "pricing.db")
    blob = _node("azurerm_storage_account.s", "azure", "BlobStorage", "eastus",
                 {"dataOutGb": 0.00008})
    blob["nodeType"] = "storage"
    apim = _node("azurerm_api_management.a", "azure", "APIManagement", "eastus",
                 {"dataOutGb": 0.00004})
    apim["nodeType"] = "routing"
    model = {
        "version": "1.0",
        "workflow": {"name": "w", "entry": "a",
                     "frequency": {"unit": "perMonth", "value": 1_000_000}},
        "nodes": {"a": apim, "s": blob},
        "edges": [{"from": "a", "to": "s", "type": "sync", "rate": 1}],
    }
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        costs = CostEngine(model, catalog=catalog, time_basis="monthly").compute()
    assert [w for w in caught if isinstance(w.message, UnpricedMetricWarning)] == []
    # 80 GB + 40 GB = 120 GB, of which 100 GB are free: 20 GB at $0.087.
    assert costs["a"] + costs["s"] == pytest.approx(20 * 0.087, rel=1e-6)


# --- Azure Retail Prices API fallback (#376) ------------------------------------


@pytest.mark.parametrize("region,price", [("eastus2", 0.05), ("canadacentral", 0.055)])
def test_missing_azure_meter_falls_back_to_the_retail_prices_api(creds, region, price):
    rows = _sync("Blob-Hot-LRS-Write-Operation", region)
    assert [(r.source, r.service, r.region, r.unit) for r in rows] == [
        ("azure-retail", "BlobStorage", region, "requests")]
    assert rows[0].price_usd == pytest.approx(price / 10_000)
    assert rows[0].attributes["meterName"] == "Hot LRS Write Operations"


def test_retail_fallback_filters_by_the_descriptor_attributes(creds):
    captured = []
    with patch.object(ic.requests, "post", side_effect=_fake_post()), \
            patch.object(ic.requests, "get", side_effect=_fake_get(captured=captured)):
        ic.InfracostClient().sync_to_cache(MagicMock(), "Blob-Hot-LRS-Write-Operation",
                                           "eastus2")
    url, params = captured[0]
    assert url == "https://prices.azure.com/api/retail/prices"
    assert params["$filter"] == (
        "serviceName eq 'Storage' and armRegionName eq 'eastus2'"
        " and priceType eq 'Consumption' and productName eq 'General Block Blob v2'"
        " and skuName eq 'Hot LRS' and meterName eq 'Hot LRS Write Operations'")


def test_retail_items_without_a_price_or_meter_are_skipped(creds):
    items = _retail("eastus2", "Storage", "Storage", "General Block Blob v2", "Hot LRS",
                    "Hot LRS Write Operations", "10K", [(0.0, 0.05)])
    no_price = {k: v for k, v in items[0].items() if k != "retailPrice"}
    no_meter = {k: v for k, v in items[0].items() if k != "meterId"}
    rows = _sync("Blob-Hot-LRS-Write-Operation", "eastus2",
                 retail=[no_price, no_meter] + items)
    assert [r.price_usd for r in rows] == [pytest.approx(0.000005)]


def test_no_retail_fallback_when_infracost_has_the_meter(creds):
    with patch.object(ic.requests, "post", side_effect=_fake_post()), \
            patch.object(ic.requests, "get") as get:
        ic.InfracostClient().sync_to_cache(MagicMock(), "Blob-Hot-LRS-Write-Operation",
                                           "eastus")
    get.assert_not_called()


def test_no_retail_fallback_for_gcp(creds):
    with patch.object(ic.requests, "post", side_effect=_fake_post()), \
            patch.object(ic.requests, "get") as get:
        assert _sync("Firestore-Read", "mars-north1") == []
    get.assert_not_called()


def test_retail_rows_supersede_seed_rows_and_a_later_sync_replaces_them(creds, tmp_path):
    cache = PricingCache(db_path=tmp_path / "pricing.db", seed=True)
    client = ic.InfracostClient()
    for price in (0.05, 0.06):
        retail = _retail("eastus", "Storage", "Storage", "General Block Blob v2", "Hot LRS",
                         "Hot LRS Write Operations", "10K", [(0.0, price)])
        catalogue = [p for p in CATALOGUE if "Write" not in str(p["attributes"])]
        with patch.object(ic.requests, "post", side_effect=_fake_post(catalogue)), \
                patch.object(ic.requests, "get", side_effect=_fake_get(retail)):
            client.sync_to_cache(cache, "Blob-Hot-LRS-Write-Operation", "eastus")
    result = cache.query("azure", "BlobStorage", "eastus", "Blob-Hot-LRS-Write-Operation")
    assert (result.source, result.price_usd) == ("azure-retail", pytest.approx(0.000006))


# --- GCP free tiers (#373) -------------------------------------------------------


def test_cloud_run_free_tier_covers_the_billing_account():
    from infra_cost_model.pricing.free_tiers import ACCOUNT, free_tier_scope

    for metric in ("CloudRun-Request", "CloudRun-vCPU-Second", "CloudRun-GiB-Second"):
        assert free_tier_scope("gcp", "CloudRun", metric) == ACCOUNT


def test_retail_query_rejects_a_filter_key_that_is_not_a_field_name():
    from infra_cost_model.pricing.sources.azure_retail import query_azure_retail_prices

    with patch.object(ic.requests, "get", side_effect=_fake_get([])) as get, \
            pytest.raises(ValueError):
        query_azure_retail_prices("Storage", "eastus", [
            {"key": "meterName eq 'x' or serviceName", "value": "Storage"}])
    get.assert_not_called()


def test_replacing_needs_a_source(tmp_path):
    cache = PricingCache(db_path=tmp_path / "pricing.db")
    with pytest.raises(ValueError):
        with cache.replacing("azure", "BlobStorage", "eastus", "m", ()):
            pass
