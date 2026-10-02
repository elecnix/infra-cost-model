"""GCP resources of a Pulumi stack export resolve to their handler (#396).

`extract_resources_from_pulumi` names each resource by its `id`, as in
`assets-bucket-93a1f2`, which says nothing about the resource type. The GCP
handlers match Terraform and CDK addresses, so every GCP resource of a real
stack export landed in the unsupported list. The type comes from the `urn`,
where the Pulumi type token is a whole segment.
"""
import warnings

import pytest

from infra_cost_model.resources.gcp import (
    CloudFunction, CloudFunctionGen2, CloudRun, CloudStorage, Firestore, matches_gcp_type,
)
from infra_cost_model.resources.registry import (
    ResourceRegistry, extract_resources_from_pulumi,
)

STACK = {
    "version": 3,
    "deployment": {
        "manifest": {"time": "2026-09-24T12:00:00Z", "magic": "abcdef",
                     "version": "3.142.0"},
        "resources": [
            {
                "urn": "urn:pulumi:prod::shop::gcp:storage/bucket:Bucket::assets",
                "custom": False,
                "type": "gcp:storage/bucket:Bucket",
                "id": "assets-bucket-93a1f2",
                "inputs": {"location": "US-CENTRAL1", "storageClass": "STANDARD",
                           "forceDestroy": True},
                "outputs": {"location": "US-CENTRAL1", "selfLink": "https://x/assets"},
            },
            {
                "urn": "urn:pulumi:prod::shop::gcp:cloudfunctions/function:Function::api",
                "custom": False,
                "type": "gcp:cloudfunctions/function:Function",
                "id": "projects/shop/locations/us-central1/functions/api",
                "inputs": {"region": "us-central1", "availableMemoryMb": 256,
                           "runtime": "python311", "timeout": 60},
                "outputs": {"httpsTriggerUrl": "https://x/api"},
            },
            {
                "urn": "urn:pulumi:prod::shop::gcp:compute/instance:Instance::vm",
                "custom": False,
                "type": "gcp:compute/instance:Instance",
                "id": "projects/shop/zones/us-central1-a/instances/vm",
                "inputs": {"machineType": "e2-medium", "zone": "us-central1-a"},
            },
        ],
    },
}


def extract(stack) -> dict:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return extract_resources_from_pulumi(stack)


def test_bucket_and_function_are_extracted():
    nodes = extract(STACK)
    # The compute instance has no handler, so only the two priced resources
    # are extracted.
    assert set(nodes) == {"assets-bucket-93a1f2",
                          "projects/shop/locations/us-central1/functions/api"}
    bucket = nodes["assets-bucket-93a1f2"]
    assert bucket["nodeType"] == "storage"
    assert bucket["service"] == "CloudStorage"
    assert bucket["region"] == "us-central1"
    assert bucket["config"] == {"location": "US-CENTRAL1", "storageClass": "STANDARD"}

    function = nodes["projects/shop/locations/us-central1/functions/api"]
    assert function["nodeType"] == "compute"
    assert function["service"] == "CloudFunctions"
    assert function["region"] == "us-central1"
    assert function["config"] == {"memoryMb": 256, "timeout": 60, "runtime": "python311"}


def test_the_node_address_names_the_type_so_the_pricing_layer_finds_it():
    """The extracted `resourceAddress` still resolves a handler."""
    nodes = extract(STACK)
    bucket = nodes["assets-bucket-93a1f2"]["resourceAddress"]
    assert ResourceRegistry.from_address(bucket) is CloudStorage
    assert ResourceRegistry.resolve_catalog_metric(
        bucket, "storageGb", {"location": "US-CENTRAL1"}) == "GCS-Standard-GiB-Month"
    function = nodes["projects/shop/locations/us-central1/functions/api"]["resourceAddress"]
    assert ResourceRegistry.from_address(function) is CloudFunction


def test_function_derives_its_catalog_quantities():
    node = extract(STACK)["projects/shop/locations/us-central1/functions/api"]
    derived = CloudFunction().derive_catalog_usage(
        {"invocations": 100.0, "avgDurationMs": 200.0, "memoryMb": 256.0}, node["config"])
    assert derived.quantities["CloudFunctions-Invocation"] == 100.0
    assert derived.quantities["CloudFunctions-GB-Second"] == pytest.approx(
        100 * 256 / 1024 * 0.2)


def test_an_unknown_gcp_type_is_reported_as_unsupported():
    with pytest.warns(UserWarning, match=r"projects/shop/zones/us-central1-a/instances/vm"):
        extract_resources_from_pulumi(STACK)


# (handler, Pulumi type token, a Terraform and a CDK address)
TYPES = [
    (CloudFunction, "gcp:cloudfunctions/function:Function",
     "google_cloudfunctions_function.api", "google::CloudFunctions::Function:Func"),
    (CloudFunctionGen2, "gcp:cloudfunctionsv2/function:Function",
     "google_cloudfunctions2_function.api", None),
    (CloudStorage, "gcp:storage/bucket:Bucket",
     "google_storage_bucket.assets", "Storage::Bucket:Assets"),
    (CloudRun, "gcp:cloudrun/service:Service",
     "google_cloud_run_service.api", "CloudRun::Service:Api"),
    (Firestore, "gcp:firestore/database:Database",
     "google_firestore_database.orders", "Firestore::Database:Orders"),
]


@pytest.mark.parametrize("handler,token,tf_address,cdk_address", TYPES,
                         ids=[t[0].__name__ for t in TYPES])
def test_a_handler_matches_its_type_token_and_its_old_addresses(
        handler, token, tf_address, cdk_address):
    assert handler.from_address(f"urn:pulumi:prod::shop::{token}::resource-name") is not None
    assert handler.from_address(tf_address) is not None
    if cdk_address:
        assert handler.from_address(cdk_address) is not None


@pytest.mark.parametrize("handler,token,tf_address,cdk_address", TYPES,
                         ids=[t[0].__name__ for t in TYPES])
def test_a_handler_does_not_match_another_gcp_type(handler, token, tf_address, cdk_address):
    other = next(t[1] for t in TYPES if t[1] != token)
    assert handler.from_address(f"urn:pulumi:prod::shop::{other}::name") is None
    assert not matches_gcp_type(f"urn:pulumi:prod::shop::{other}::name", handler)


def test_the_id_alone_names_no_type():
    """A Pulumi `id` says nothing about the resource type."""
    assert CloudStorage.from_address("assets-bucket-93a1f2") is None
    assert CloudFunction.from_address(
        "projects/shop/locations/us-central1/functions/api") is None


def test_an_id_with_a_provider_prefixed_type_still_matches():
    """A handler matched on its Terraform address inside an id keeps working."""
    assert CloudStorage.from_address("google:storage:Bucket:assets") is not None
