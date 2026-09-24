"""GCP resource model stubs.

Per DP#6, the cost model supports multi-cloud. These stubs provide the
handler interface for GCP resources with the same from_address / extract
pattern used for AWS. Full pricing implementations will be added as the
model is validated against real GCP pricing data.
"""

import math
from typing import Optional

from .types import (
    ComputeResource, DerivedCatalogUsage, StorageResource, RoutingResource, ResourceExtract,
)

# Cloud Run functions (1st gen) memory sizes in MB, and the CPU clock in GHz
# that each one gets. A function is billed for the smallest size that holds its
# memory.
_FUNCTION_CPU_GHZ = ((128, 0.2), (256, 0.4), (512, 0.8), (1024, 1.4),
                     (2048, 2.4), (4096, 4.8), (8192, 4.8))


class CloudFunction(ComputeResource):
    """GCP Cloud Function - compute node (equivalent to AWS Lambda)."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["invocations", "avgDurationMs", "memoryMb"]

    def derive_catalog_usage(self, usage: dict[str, float]) -> Optional[DerivedCatalogUsage]:
        """Derive invocations, GB-seconds and GHz-seconds for a 1st gen function.

        GCP bills the memory size that holds ``memoryMb`` and the CPU clock
        that comes with it, for the duration rounded up to the next 100 ms,
        and at least 100 ms.
        """
        inputs = ("invocations", "avgDurationMs", "memoryMb")
        if not all(name in usage for name in inputs):
            return None
        invocations = usage["invocations"]
        memory_mb, ghz = next(
            ((mb, ghz) for mb, ghz in _FUNCTION_CPU_GHZ if usage["memoryMb"] <= mb),
            _FUNCTION_CPU_GHZ[-1])
        # Each invocation bills at least one 100 ms increment.
        seconds = max(math.ceil(usage["avgDurationMs"] / 100), 1) / 10
        return DerivedCatalogUsage(
            consumed=frozenset(inputs),
            quantities={
                "CloudFunctions-Invocation": invocations,
                "CloudFunctions-GB-Second": invocations * memory_mb / 1024 * seconds,
                "CloudFunctions-GHz-Second": invocations * ghz * seconds,
            },
        )

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["CloudFunction"]:
        if (resource_address.startswith("google_cloudfunctions_function.") or
                "google:cloudfunctions:Function:" in resource_address or
                "CloudFunctions::Function:" in resource_address):
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="compute",
            provider="gcp",
            service="CloudFunctions",
            region=values.get("region"),
            config={
                "memoryMb": values.get("available_memory_mb"),
                "timeout": values.get("timeout"),
                "runtime": values.get("runtime"),
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="compute",
            provider="gcp",
            service="CloudFunctions",
            region=inputs.get("region"),
            config={
                "memoryMb": inputs.get("availableMemoryMb"),
                "timeout": inputs.get("timeout"),
                "runtime": inputs.get("runtime"),
            },
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        properties = resource.get("Properties", {})
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="compute",
            provider="gcp",
            service="CloudFunctions",
            region=None,
            config={
                "memoryMb": properties.get("AvailableMemoryMb"),
                "timeout": properties.get("Timeout"),
                "runtime": properties.get("Runtime"),
            },
        )


class CloudStorage(StorageResource):
    """GCP Cloud Storage bucket - storage node (equivalent to AWS S3)."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["storageGb", "readRequests", "writeRequests", "dataOutGb"]

    @property
    def catalog_metrics(self) -> dict[str, str]:
        # Standard class in one region. Writes are Class A operations and
        # reads are Class B operations.
        # GCP bills egress under each service's own SKUs (#372).
        return {"storageGb": "GCS-Standard-GiB-Month",
                "writeRequests": "GCS-Class-A-Operation",
                "readRequests": "GCS-Class-B-Operation",
                "dataOutGb": "GCS-Internet-Egress-GiB"}

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["CloudStorage"]:
        if (resource_address.startswith("google_storage_bucket.") or
                "google:storage:Bucket:" in resource_address or
                "Storage::Bucket:" in resource_address):
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="storage",
            provider="gcp",
            service="CloudStorage",
            region=values.get("location"),
            config={
                "location": values.get("location"),
                "storageClass": values.get("storage_class"),
                "versioning": values.get("versioning", {}).get("enabled"),
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="storage",
            provider="gcp",
            service="CloudStorage",
            region=inputs.get("location"),
            config={
                "location": inputs.get("location"),
                "storageClass": inputs.get("storageClass"),
            },
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        properties = resource.get("Properties", {})
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="storage",
            provider="gcp",
            service="CloudStorage",
            region=properties.get("Location"),
            config={
                "location": properties.get("Location"),
                "storageClass": properties.get("StorageClass"),
            },
        )


class CloudRun(RoutingResource):
    """GCP Cloud Run service - routing node (equivalent to API Gateway)."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["requests", "dataOutGb", "vcpuSeconds", "memoryGbSeconds"]

    @property
    def catalog_metrics(self) -> dict[str, str]:
        # Request-based billing.
        return {"requests": "CloudRun-Request",
                "vcpuSeconds": "CloudRun-vCPU-Second",
                "memoryGbSeconds": "CloudRun-GiB-Second",
                "dataOutGb": "CloudRun-Internet-Egress-GiB"}

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["CloudRun"]:
        if (resource_address.startswith("google_cloud_run_service.") or
                "google:cloudrun:Service:" in resource_address or
                "CloudRun::Service:" in resource_address):
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="routing",
            provider="gcp",
            service="CloudRun",
            region=values.get("location"),
            config={
                "location": values.get("location"),
                "ingress": values.get("traffic", [{}])[0].get("percent") if values.get("traffic") else None,
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="routing",
            provider="gcp",
            service="CloudRun",
            region=inputs.get("location"),
            config={"location": inputs.get("location")},
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        properties = resource.get("Properties", {})
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="routing",
            provider="gcp",
            service="CloudRun",
            region=properties.get("Location"),
            config={"location": properties.get("Location")},
        )


class Firestore(StorageResource):
    """GCP Firestore - storage node (equivalent to DynamoDB)."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["readRequests", "writeRequests", "storageGb"]

    @property
    def catalog_metrics(self) -> dict[str, str]:
        return {"readRequests": "Firestore-Read", "writeRequests": "Firestore-Write",
                "storageGb": "Firestore-GiB-Month"}

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["Firestore"]:
        if (resource_address.startswith("google_firestore_database.") or
                "google:firestore:Database:" in resource_address or
                "Firestore::Database:" in resource_address):
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="storage",
            provider="gcp",
            service="Firestore",
            region=values.get("location_id"),
            config={
                "location": values.get("location_id"),
                "type": values.get("type"),
                "concurrencyMode": values.get("concurrency_mode"),
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="storage",
            provider="gcp",
            service="Firestore",
            region=inputs.get("locationId"),
            config={
                "location": inputs.get("locationId"),
                "type": inputs.get("type"),
            },
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        properties = resource.get("Properties", {})
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="storage",
            provider="gcp",
            service="Firestore",
            region=properties.get("LocationId"),
            config={"location": properties.get("LocationId")},
        )
