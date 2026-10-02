"""Multi-region and dual-region Cloud Storage buckets have catalog rows (#397).

A bucket's location is the catalog region: `us`, `eu` or `asia` for a
multi-region, and the code of a pair such as `nam4` for a dual-region. The
Cloud Pricing API holds multi-region storage under the regions `us` and
`asia` ("Standard Storage US Multi-region") and a dual-region product under
each GCP region, beside the regional one ("Standard Storage Iowa
Dual-region" in us-central1), and holds the operations of both kinds in its
global catalogue ("Multi-Region Standard Class A Operations", "Dual-Region
Standard Class A Operations").

GCP gives the three multi-regions one price each
(https://cloud.google.com/storage/pricing, checked 2026-10-02: Standard
storage $0.026 a GiB-month under "Multi-region", whatever the region) and
one price to every dual-region (the same page: "Standard storage in a
dual-region comprised of Iowa and Oregon will be billed at $0.022 per GB per
month for the us-central1 dual-region SKU and $0.022 per GB per month for
the us-west1 dual-region SKU"), so the sync reads the `us` product for `eu`
and one region's dual-region product for every dual-region code.

The fake catalogue below holds the products that the live API returned on
2026-09-24, with the prices of the pricing page. It answers a query the way
the live API does: it keeps the products of the vendor, service and region
whose product family and attributes equal the filter values.
"""

import warnings
from unittest.mock import MagicMock, patch

import pytest

from infra_cost_model.engine.engine import CostEngine
from infra_cost_model.pricing.cache import PricingCache
from infra_cost_model.pricing.catalog import PricingCatalog
from infra_cost_model.pricing.sources import infracost as ic
from infra_cost_model.resources.gcp import CloudStorage
from infra_cost_model.resources.registry import extract_resources_from_tf

# Prices of https://cloud.google.com/storage/pricing (checked 2026-10-02).
# The page states one multi-region rate and one dual-region rate for every
# class, so `us`, `eu` and `asia` cost the same.
MULTI_REGION = 0.026
DUAL_REGION = 0.022
IOWA_REGIONAL = 0.020
CLASS_A_MULTI = 0.00001
CLASS_B_MULTI = 0.0000004


def _product(region, family, description, group, prices):
    return {
        "vendor": "gcp", "service": "Cloud Storage", "region": region,
        "productFamily": family,
        "attributes": [{"key": "description", "value": description},
                       {"key": "resourceGroup", "value": group}],
        "prices": [{"USD": usd, "unit": unit, "startUsageAmount": start,
                    "endUsageAmount": end} for usd, unit, start, end in prices],
    }


CATALOGUE = [
    _product("us-central1", "Storage", "Standard Storage Iowa", "RegionalStorage",
             [("0", "gibibyte month", "0", "5"), ("0.020", "gibibyte month", "5", None)]),
    _product("us-central1", "Storage", "Standard Storage Iowa Dual-region",
             "MultiRegionalStorage", [("0.022", "gibibyte month", "0", None)]),
    _product("us-central1", "Storage", "Nearline Storage Iowa Dual-region",
             "NearlineStorage", [("0.011", "gibibyte month", "0", None)]),
    _product("us", "Storage", "Standard Storage US Multi-region",
             "MultiRegionalStorage", [("0.026", "gibibyte month", "0", None)]),
    _product("asia", "Storage", "Standard Storage Asia Multi-region",
             "MultiRegionalStorage", [("0.026", "gibibyte month", "0", None)]),
    _product("global", "Storage", "Regional Standard Class A Operations", "RegionalOps",
             [("0", "count", "0", "5000"), ("0.000005", "count", "5000", None)]),
    _product("global", "Storage", "Regional Standard Class B Operations", "RegionalOps",
             [("0", "count", "0", "50000"), ("0.0000004", "count", "50000", None)]),
    _product("global", "Storage", "Multi-Region Standard Class A Operations", "StandardOps",
             [("0.00001", "count", "0", None)]),
    _product("global", "Storage", "Multi-Region Standard Class B Operations", "StandardOps",
             [("0.0000004", "count", "0", None)]),
    _product("global", "Storage", "Dual-Region Standard Class A Operations", "StandardOps",
             [("0.00001", "count", "0", None)]),
    _product("global", "Storage", "Dual-Region Standard Class B Operations", "StandardOps",
             [("0.0000004", "count", "0", None)]),
    _product("global", "Storage", "Multi-Region Nearline Class A Operations", "NearlineOps",
             [("0.00002", "count", "0", None)]),
    _product("global", "Network",
             "Download Worldwide Destinations (excluding Asia & Australia)",
             "PremiumInternetEgress",
             [("0", "gibibyte", "0", "100"), ("0.12", "gibibyte", "100", "10240"),
              ("0.11", "gibibyte", "10240", "153600"), ("0.08", "gibibyte", "153600", None)]),
]


def _fake_post(captured=None):
    """A `requests.post` stand-in that filters the catalogue like the API."""
    def post(url, headers=None, json=None, timeout=None):
        variables = json["variables"]
        if captured is not None:
            captured.append(variables)
        filters = variables.get("attributeFilters") or []
        matched = []
        for product in CATALOGUE:
            if (product["vendor"], product["service"], product["region"]) != (
                    variables["vendorName"], variables["service"], variables["region"]):
                continue
            attrs = {a["key"]: a["value"] for a in product["attributes"]}
            if all(attrs.get(f["key"]) == f["value"] for f in filters):
                matched.append(product)
        response = MagicMock(status_code=200)
        response.json.return_value = {"data": {"products": matched}}
        response.raise_for_status.return_value = None
        return response
    return post


GCS_METRICS = [metric for metric, descriptor in ic.METRIC_DESCRIPTORS.items()
               if descriptor.get("store_service") == "CloudStorage"]


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("INFRACOST_API_KEY", "test-token")
    monkeypatch.setenv("INFRACOST_ORG_ID", "org-123")


def synced_catalog(tmp_path, regions):
    """A catalog with the Cloud Storage rows of *regions*, from the fake API."""
    cache = PricingCache(db_path=tmp_path / "pricing.db")
    client = ic.InfracostClient(api_key="test-token", org_id="org-123")
    with patch.object(ic.requests, "post", side_effect=_fake_post()):
        for region in regions:
            for metric in GCS_METRICS:
                client.sync_to_cache(cache, metric, region)
    return PricingCatalog(db_path=tmp_path / "pricing.db")


def one_bucket(location, storage_gb=1024, writes=10_000, reads=100_000, egress_gb=0):
    """The model of one Standard bucket in *location*, priced for a month."""
    address = "google_storage_bucket.b"
    usage = {
        "storageGb": {"unit": "gibibyte month", "value": storage_gb, "fixed": True},
        "writeRequests": {"unit": "count", "value": writes},
        "readRequests": {"unit": "count", "value": reads},
    }
    if egress_gb:
        usage["dataOutGb"] = {"unit": "gibibyte", "value": egress_gb}
    node = {
        "nodeType": "storage", "resourceAddress": address, "provider": "gcp",
        "service": "CloudStorage", "region": location.lower(),
        "config": {"location": location, "storageClass": "STANDARD"},
        "usageMetrics": usage,
    }
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


def test_us_multi_region_bucket_is_priced(tmp_path):
    catalog = synced_catalog(tmp_path, ["us"])
    address, model = one_bucket("US", egress_gb=200)
    costs, engine = compute(model, catalog)
    assert costs[address] == pytest.approx(
        1024 * MULTI_REGION + 10_000 * CLASS_A_MULTI + 100_000 * CLASS_B_MULTI
        + 200 * 0.12)
    assert engine.unpriced_metrics == []


def test_asia_multi_region_bucket_is_priced(tmp_path):
    catalog = synced_catalog(tmp_path, ["asia"])
    address, model = one_bucket("ASIA")
    costs, engine = compute(model, catalog)
    assert costs[address] == pytest.approx(
        1024 * MULTI_REGION + 10_000 * CLASS_A_MULTI + 100_000 * CLASS_B_MULTI)
    assert engine.unpriced_metrics == []


def test_eu_multi_region_bucket_is_priced(tmp_path):
    """The catalogue has no `eu` product, and GCP prices `eu` like `us`."""
    catalog = synced_catalog(tmp_path, ["eu"])
    address, model = one_bucket("EU")
    costs, engine = compute(model, catalog)
    assert costs[address] == pytest.approx(
        1024 * MULTI_REGION + 10_000 * CLASS_A_MULTI + 100_000 * CLASS_B_MULTI)
    assert engine.unpriced_metrics == []


def test_nam4_dual_region_bucket_is_priced(tmp_path):
    catalog = synced_catalog(tmp_path, ["nam4"])
    address, model = one_bucket("NAM4")
    costs, engine = compute(model, catalog)
    assert costs[address] == pytest.approx(
        1024 * DUAL_REGION + 10_000 * CLASS_A_MULTI + 100_000 * CLASS_B_MULTI)
    assert engine.unpriced_metrics == []


def test_regional_bucket_is_still_priced(tmp_path):
    """Iowa keeps its Always Free 5 GB-months; the location kinds didn't move."""
    catalog = synced_catalog(tmp_path, ["us-central1"])
    address, model = one_bucket("US-CENTRAL1", storage_gb=8, writes=1, reads=1)
    costs, engine = compute(model, catalog)
    # Three GiB-months above the Always Free five, and operations inside it.
    assert costs[address] == pytest.approx(3 * IOWA_REGIONAL)
    assert engine.unpriced_metrics == []


def test_each_location_reads_the_catalogue_region_that_prices_it():
    """`eu` reads the `us` product, and a dual-region code reads Iowa's."""
    captured = []
    with patch.object(ic.requests, "post", side_effect=_fake_post(captured)):
        for region in ("us", "eu", "nam4"):
            cache = MagicMock()
            ic.InfracostClient(api_key="t", org_id="o").sync_to_cache(
                cache,
                "GCS-Standard-MultiRegion-GiB-Month" if region in ("us", "eu")
                else "GCS-Standard-DualRegion-GiB-Month",
                region)
    assert {variables["region"] for variables in captured} == {"us", "us-central1"}


def test_eu_reads_the_us_product(tmp_path):
    cache = PricingCache(db_path=tmp_path / "pricing.db")
    client = ic.InfracostClient(api_key="test-token", org_id="org-123")
    with patch.object(ic.requests, "post", side_effect=_fake_post()):
        assert client.sync_to_cache(cache, "GCS-Standard-MultiRegion-GiB-Month", "eu") == 1
    rows = cache.query("gcp", "CloudStorage", "eu", "GCS-Standard-MultiRegion-GiB-Month")
    assert rows.price_usd == pytest.approx(MULTI_REGION)


def test_nearline_dual_region_selects_its_own_product(tmp_path):
    cache = PricingCache(db_path=tmp_path / "pricing.db")
    client = ic.InfracostClient(api_key="test-token", org_id="org-123")
    with patch.object(ic.requests, "post", side_effect=_fake_post()):
        client.sync_to_cache(cache, "GCS-Nearline-DualRegion-GiB-Month", "nam4")
    rows = cache.query("gcp", "CloudStorage", "nam4", "GCS-Nearline-DualRegion-GiB-Month")
    assert rows.price_usd == pytest.approx(0.011)


def test_sync_regions_cover_every_location_the_catalogue_prices():
    regions = ic.sync_regions("gcp")
    for location in ("us", "eu", "asia", "nam4", "eur4", "eur5", "eur7", "eur8", "asia1"):
        assert location in regions


def test_a_sync_of_a_location_reads_only_the_location_metrics(creds, monkeypatch):
    """A location is not a GCP region, so no other product is looked up there."""
    synced = []

    def fake_sync(self, cache, metric, region, vendor="aws"):
        synced.append((metric, region))
        return 1

    monkeypatch.setattr(ic.InfracostClient, "sync_to_cache", fake_sync)
    monkeypatch.setattr("infra_cost_model.pricing.cache.PricingCache",
                        lambda *a, **k: MagicMock())
    ic.sync_pricing_catalog(vendor="gcp", regions=["nam4"])
    metrics = {metric for metric, region in synced}
    assert metrics == set(GCS_METRICS)
    assert {region for metric, region in synced} == {"nam4"}


def test_every_location_metric_has_a_descriptor():
    for location in ("US", "NAM4"):
        for storage_class in ("STANDARD", "NEARLINE", "COLDLINE", "ARCHIVE"):
            metrics = CloudStorage().catalog_metrics_for(
                {"location": location, "storageClass": storage_class})
            for metric in metrics.values():
                assert ic.METRIC_DESCRIPTORS[metric]["store_service"] == "CloudStorage"


@pytest.mark.parametrize("location", ["US", "EU", "ASIA", "NAM4", "US-CENTRAL1"])
def test_a_priced_location_does_not_warn(location):
    resource = {"address": "google_storage_bucket.b", "type": "google_storage_bucket",
                "values": {"location": location, "storage_class": "STANDARD"}}
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        extract_resources_from_tf({"resource": [resource]})
    assert [str(w.message) for w in record] == []


def test_a_configurable_dual_region_warns():
    """A pair of regions a bucket names itself has no rows in the catalogue."""
    resource = {"address": "google_storage_bucket.b", "type": "google_storage_bucket",
                "values": {"location": "US-CENTRAL1+US-WEST1", "storage_class": "STANDARD"}}
    with pytest.warns(UserWarning, match=r"google_storage_bucket.b.*dual-region.*unpriced"):
        extract_resources_from_tf({"resource": [resource]})


def test_an_unknown_storage_class_still_warns():
    resource = {"address": "google_storage_bucket.b", "type": "google_storage_bucket",
                "values": {"location": "NAM4", "storage_class": "GLACIAL"}}
    with pytest.warns(UserWarning, match="GLACIAL"):
        extract_resources_from_tf({"resource": [resource]})