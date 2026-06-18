"""GCP resource model stubs.

Per DP#6, the cost model supports multi-cloud. These stubs provide the
handler interface for GCP resources with the same from_address / extract
pattern used for AWS. Full pricing implementations will be added as the
model is validated against real GCP pricing data.
"""

from typing import Optional

from .types import ComputeResource, StorageResource, RoutingResource, ResourceExtract


class CloudFunction(ComputeResource):
    """GCP Cloud Function - compute node (equivalent to AWS Lambda)."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["invocations", "avgDurationMs", "memoryMb"]

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["CloudFunction"]:
        if (resource_address.startswith("google_cloudfunctions_function.") or
                "google:cloudfunctions:Function:" in resource_address or
                "CloudFunctions::Function" in resource_address):
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

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["CloudStorage"]:
        if (resource_address.startswith("google_storage_bucket.") or
                "google:storage:Bucket:" in resource_address or
                "Storage::Bucket" in resource_address):
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

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["CloudRun"]:
        if (resource_address.startswith("google_cloud_run_service.") or
                "google:cloudrun:Service:" in resource_address or
                "CloudRun::Service" in resource_address):
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

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["Firestore"]:
        if (resource_address.startswith("google_firestore_database.") or
                "google:firestore:Database:" in resource_address or
                "Firestore::Database" in resource_address):
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
