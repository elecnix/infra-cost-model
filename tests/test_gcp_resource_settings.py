"""GCP handlers price the product that the resource settings select (#375).

Cloud Storage reads the storage class of a bucket, and warns for dual-region
and multi-region locations, which have no catalog rows yet. A Cloud Run
function (2nd gen) bills as a Cloud Run service: requests, vCPU-seconds and
GiB-seconds at Cloud Run rates.
"""
import pytest

from infra_cost_model.pricing.sources import infracost as ic
from infra_cost_model.resources.gcp import CloudFunctionGen2, CloudStorage, gcs_location_type
from infra_cost_model.resources.registry import (
    ResourceRegistry, extract_resources_from_tf,
)


# --- Cloud Storage --------------------------------------------------------------


def test_standard_regional_keeps_its_metrics():
    assert CloudStorage().catalog_metrics_for({}) == CloudStorage().catalog_metrics
    assert CloudStorage().catalog_metrics_for(
        {"storageClass": "STANDARD", "location": "us-central1"}) == CloudStorage().catalog_metrics


@pytest.mark.parametrize("storage_class,name", [
    ("NEARLINE", "Nearline"), ("coldline", "Coldline"), ("ARCHIVE", "Archive"),
])
def test_storage_class_selects_its_rows(storage_class, name):
    metrics = CloudStorage().catalog_metrics_for(
        {"storageClass": storage_class, "location": "US-CENTRAL1"})
    assert metrics["storageGb"] == f"GCS-{name}-GiB-Month"
    assert metrics["writeRequests"] == f"GCS-{name}-Class-A-Operation"
    assert metrics["readRequests"] == f"GCS-{name}-Class-B-Operation"
    for metric in metrics.values():
        assert ic.METRIC_DESCRIPTORS[metric]["store_service"] == "CloudStorage"


def test_class_descriptors_select_the_regional_product():
    storage = ic.METRIC_DESCRIPTORS["GCS-Nearline-GiB-Month"]
    assert storage["attribute_filters"] == [{"key": "resourceGroup", "value": "NearlineStorage"}]
    pattern = storage["attribute_patterns"]["description"]
    import re
    assert re.fullmatch(pattern, "Nearline Storage Iowa")
    assert not re.fullmatch(pattern, "Nearline Storage Iowa Dual-region")
    ops = ic.METRIC_DESCRIPTORS["GCS-Archive-Class-A-Operation"]
    assert ops["query_region"] == "global"
    assert ops["attribute_filters"] == [
        {"key": "description", "value": "Regional Archive Class A Operations"}]


@pytest.mark.parametrize("location,kind", [
    ("us-central1", "region"), ("US-CENTRAL1", "region"), ("US", "multi-region"),
    ("eu", "multi-region"), ("ASIA", "multi-region"), ("NAM4", "dual-region"),
    ("EUR4", "dual-region"), (None, "region"),
])
def test_location_type(location, kind):
    assert gcs_location_type(location) == kind


def test_multi_region_bucket_warns_and_gets_its_own_metrics():
    resource = {"address": "google_storage_bucket.b", "type": "google_storage_bucket",
                "values": {"location": "US", "storage_class": "STANDARD"}}
    with pytest.warns(UserWarning, match=r"google_storage_bucket.b.*multi-region"):
        node = extract_resources_from_tf({"resource": [resource]})["google_storage_bucket.b"]
    metrics = CloudStorage().catalog_metrics_for(node["config"])
    assert metrics["storageGb"] == "GCS-Standard-MultiRegion-GiB-Month"
    assert metrics["storageGb"] not in ic.METRIC_DESCRIPTORS


def test_bucket_region_is_lower_case():
    resource = {"address": "google_storage_bucket.b", "type": "google_storage_bucket",
                "values": {"location": "US-CENTRAL1", "storage_class": "NEARLINE"}}
    node = extract_resources_from_tf({"resource": [resource]})["google_storage_bucket.b"]
    assert node["region"] == "us-central1"


def test_legacy_regional_class_is_standard():
    metrics = CloudStorage().catalog_metrics_for(
        {"storageClass": "REGIONAL", "location": "us-central1"})
    assert metrics == CloudStorage().catalog_metrics


# --- Cloud Run functions (2nd gen) ------------------------------------------------


def test_gen2_addresses_have_their_own_handler():
    assert ResourceRegistry.from_address("google_cloudfunctions2_function.api") is CloudFunctionGen2
    assert ResourceRegistry.from_address("google_cloudfunctions_function.api") is not CloudFunctionGen2


def test_gen2_bills_cloud_run_quantities():
    # 512 MB gets 0.333 vCPU by default. 250 ms rounds up to 300 ms.
    derived = CloudFunctionGen2().derive_catalog_usage(
        {"invocations": 10.0, "avgDurationMs": 250.0, "memoryMb": 512.0}, {})
    assert derived.quantities == {
        "CloudRun-Request": 10.0,
        "CloudRun-vCPU-Second": pytest.approx(10 * 0.333 * 0.3),
        "CloudRun-GiB-Second": pytest.approx(10 * 0.5 * 0.3),
    }
    for metric in derived.quantities:
        assert CloudFunctionGen2().catalog_services[metric] == "CloudRun"


def test_gen2_cpu_comes_from_the_config():
    derived = CloudFunctionGen2().derive_catalog_usage(
        {"invocations": 1.0, "avgDurationMs": 1000.0, "memoryMb": 512.0}, {"cpu": 2})
    assert derived.quantities["CloudRun-vCPU-Second"] == pytest.approx(2.0)


def test_gen2_terraform_extraction():
    resource = {"address": "google_cloudfunctions2_function.api",
                "type": "google_cloudfunctions2_function",
                "values": {"location": "us-central1",
                           "build_config": [{"runtime": "python312"}],
                           "service_config": [{"available_memory": "1Gi",
                                               "available_cpu": "1", "timeout_seconds": 60}]}}
    node = extract_resources_from_tf({"resource": [resource]})[
        "google_cloudfunctions2_function.api"]
    assert node["service"] == "CloudFunctions"
    assert node["region"] == "us-central1"
    assert node["config"] == {"generation": 2, "memoryMb": 1024, "cpu": 1.0,
                              "timeout": 60, "runtime": "python312"}


def test_gen2_pulumi_extraction():
    extract = CloudFunctionGen2.extract_pulumi({
        "id": "projects/p/locations/us-central1/functions/api",
        "type": "gcp:cloudfunctionsv2/function:Function",
        "inputs": {"location": "us-central1",
                   "serviceConfig": {"availableMemory": "256M"},
                   "buildConfig": {"runtime": "nodejs20"}}})
    assert extract.config["memoryMb"] == 256
    assert extract.config["cpu"] is None


def test_gen2_function_is_priced_at_cloud_run_rates(tmp_path):
    """With the Cloud Run rows, a gen2 function is priced like a service."""
    import warnings

    from infra_cost_model.engine.engine import CostEngine
    from infra_cost_model.pricing.cache import Price
    from infra_cost_model.pricing.catalog import PricingCatalog

    catalog = PricingCatalog(db_path=tmp_path / "pricing.db")
    for metric, price in [("CloudRun-Request", 0.0000004), ("CloudRun-vCPU-Second", 0.000024),
                          ("CloudRun-GiB-Second", 0.0000025)]:
        catalog._cache.upsert(Price(
            vendor="gcp", service="CloudRun", region="us-central1", product_family=None,
            attributes={}, usage_metric=metric, unit="x", price_usd=price, source="test"))
    address = "google_cloudfunctions2_function.api"
    model = {"version": "1.0",
             "workflow": {"name": "w", "entry": address,
                          "frequency": {"unit": "perMonth", "value": 1_000_000}},
             "nodes": {address: {
                 "nodeType": "compute", "resourceAddress": address, "provider": "gcp",
                 "service": "CloudFunctions", "region": "us-central1",
                 "config": {"cpu": 1},
                 "usageMetrics": {"invocations": {"unit": "requests", "value": 1},
                                  "avgDurationMs": {"unit": "ms", "value": 1000},
                                  "memoryMb": {"unit": "MB", "value": 1024}}}},
             "edges": []}
    engine = CostEngine(model, catalog=catalog, time_basis="monthly")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        costs = engine.compute()
    # 1M requests, 1M vCPU-seconds and 1M GiB-seconds.
    assert costs[address] == pytest.approx(0.4 + 24.0 + 2.5)
    assert engine.unpriced_metrics == []


def test_nearline_sync_keeps_the_regional_product():
    """The dual-region product and the early-delete charge share the group."""
    from unittest.mock import MagicMock, patch

    def product(description, usd, unit):
        return {"productFamily": "Storage",
                "attributes": [{"key": "description", "value": description},
                               {"key": "resourceGroup", "value": "NearlineStorage"}],
                "prices": [{"USD": usd, "unit": unit, "startUsageAmount": "0",
                            "endUsageAmount": None}]}

    products = [product("Nearline Storage Iowa", "0.01", "gibibyte month"),
                product("Nearline Storage Iowa Dual-region", "0.011", "gibibyte month"),
                product("Nearline Storage Iowa (Early Delete)", "0.00033333", "gibibyte day")]

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
        client.sync_to_cache(cache, "GCS-Nearline-GiB-Month", "us-central1")
    assert [(r.price_usd, r.service) for r in stored] == [(0.01, "CloudStorage")]


@pytest.mark.parametrize("value,mb", [("256M", 256), ("1Gi", 1024), (512, 512), (True, None),
                                      ("lots", None), (None, None), (["256M"], None),
                                      ({"size": 1}, None)])
def test_parse_memory_mb(value, mb):
    from infra_cost_model.resources.gcp import parse_memory_mb
    assert parse_memory_mb(value) == mb


def test_unknown_class_warns_for_a_multi_region_bucket_too():
    resource = {"address": "google_storage_bucket.b", "type": "google_storage_bucket",
                "values": {"location": "EU", "storage_class": "GLACIAL"}}
    with pytest.warns(UserWarning) as record:
        extract_resources_from_tf({"resource": [resource]})
    messages = [str(w.message) for w in record]
    assert any("multi-region" in m for m in messages)
    assert any("GLACIAL" in m for m in messages)
