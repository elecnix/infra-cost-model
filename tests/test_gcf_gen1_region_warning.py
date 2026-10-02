"""A 1st gen Cloud Function in a region with no 1st gen prices warns (#400).

The Infracost API has no 1st gen Cloud Run functions CPU or memory price in 15
of the synced GCP regions, so a `google_cloudfunctions_function` there has
unpriced usage. GCP doesn't offer 1st gen functions there; the 2nd gen resource
bills at Cloud Run rates.
"""
import warnings

import pytest

from infra_cost_model.pricing.sources.infracost import (
    FUNCTIONS_GEN1_UNPRICED_REGIONS, sync_regions,
)
from infra_cost_model.resources.gcp import (
    CloudFunction, CloudFunctionGen2, Gen1FunctionRegionWarning,
)


def _tf_function(region):
    return {"address": "google_cloudfunctions_function.api",
            "values": {"region": region, "available_memory_mb": 256, "runtime": "python312"}}


def _warnings_from(func, *args):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        func(*args)
    return [str(w.message) for w in caught]


@pytest.mark.parametrize("extract", ["extract_tf", "extract_pulumi"])
@pytest.mark.parametrize("region", ["us-central1", "europe-west2", "us-east4"])
def test_supported_region_is_silent(extract, region):
    resource = ({"id": "google:cloudfunctions:Function:api", "inputs": {"region": region}}
                if extract == "extract_pulumi" else _tf_function(region))
    assert _warnings_from(getattr(CloudFunction, extract), resource) == []


@pytest.mark.parametrize("region", ["us-south1", "europe-west8"])
def test_unpriced_region_warns_and_points_at_2nd_gen(region):
    (message,) = _warnings_from(CloudFunction.extract_tf, _tf_function(region))
    assert region in message
    assert "does not offer 1st gen" in message
    assert "google_cloudfunctions2_function" in message


def test_unpriced_region_warns_from_pulumi_too():
    (message,) = _warnings_from(
        CloudFunction.extract_pulumi,
        {"id": "google:cloudfunctions:Function:api", "inputs": {"region": "us-south1"}})
    assert "us-south1" in message


@pytest.mark.parametrize("region", ["us-south1", "us-central1"])
def test_2nd_gen_never_warns(region):
    assert _warnings_from(CloudFunctionGen2.extract_tf, {
        "address": "google_cloudfunctions2_function.api",
        "values": {"location": region, "service_config": [{"available_memory": "256M"}]}}) == []


def test_missing_region_is_silent():
    assert _warnings_from(CloudFunction.extract_tf, _tf_function(None)) == []


def test_every_listed_region_is_a_synced_gcp_region():
    assert FUNCTIONS_GEN1_UNPRICED_REGIONS <= set(sync_regions("gcp"))
    assert len(FUNCTIONS_GEN1_UNPRICED_REGIONS) == 15


def test_the_warning_is_its_own_category():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        CloudFunction.extract_tf(_tf_function("us-south1"))
    assert [w.category for w in caught] == [Gen1FunctionRegionWarning]


def test_the_extraction_still_returns_the_resource():
    extract = CloudFunction.extract_tf(_tf_function("us-south1"))
    assert extract.resource_address == "google_cloudfunctions_function.api"
    assert extract.service == "CloudFunctions"
    assert extract.region == "us-south1"