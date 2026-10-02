"""A 1st gen Cloud Function in a region our catalog has no rows for warns (#400).

A `google_cloudfunctions_function` in a region listed in
`FUNCTIONS_GEN1_UNPRICED_REGIONS` has unpriced CPU and memory usage, and the
warning points at the 2nd gen resource, which is priced at Cloud Run rates in
every region. The set's membership is unverified (see the comment beside it),
so the warning states what the catalog lacks rather than what the provider
sells, and a region that is not in the set is treated as unknown.
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
    assert "no 1st gen Cloud Run functions rows" in message
    assert "google_cloudfunctions2_function" in message


def test_the_warning_claims_nothing_about_the_provider():
    """The set's membership is unverified, so the message must not assert it."""
    (message,) = _warnings_from(CloudFunction.extract_tf, _tf_function("us-south1"))
    assert "GCP does not offer" not in message
    assert "does not sell" not in message


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


def test_missing_region_is_silent_and_returns_the_resource():
    """CDK states no region, so the check cannot be made and stays quiet."""
    extract = CloudFunction.extract_tf(_tf_function(None))
    assert extract.region is None
    assert _warnings_from(CloudFunction.extract_tf, _tf_function(None)) == []


def test_cdk_extraction_states_no_region_and_does_not_warn():
    resource = {"LogicalId": "MyFn", "Properties": {"AvailableMemoryMb": 256}}
    assert _warnings_from(CloudFunction.extract_cdk, resource) == []
    assert CloudFunction.extract_cdk(resource).region is None


def test_every_listed_region_is_a_synced_gcp_region():
    assert FUNCTIONS_GEN1_UNPRICED_REGIONS <= set(sync_regions("gcp"))
    assert len(FUNCTIONS_GEN1_UNPRICED_REGIONS) == 14


def test_the_set_holds_only_region_names():
    assert all(isinstance(region, str) and region
               for region in FUNCTIONS_GEN1_UNPRICED_REGIONS)
    assert None not in FUNCTIONS_GEN1_UNPRICED_REGIONS


def test_a_supported_region_is_not_in_the_set():
    for region in ("us-central1", "europe-west2", "us-east4", "europe-west4"):
        assert region not in FUNCTIONS_GEN1_UNPRICED_REGIONS


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