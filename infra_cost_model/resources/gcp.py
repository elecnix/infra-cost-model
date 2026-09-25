"""GCP resource model stubs.

Per DP#6, the cost model supports multi-cloud. These stubs provide the
handler interface for GCP resources with the same from_address / extract
pattern used for AWS. Full pricing implementations will be added as the
model is validated against real GCP pricing data.
"""

import math
import re
import warnings
from typing import Any, Optional

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

    def derive_catalog_usage(self, usage: dict[str, float],
                             config: Optional[dict] = None) -> Optional[DerivedCatalogUsage]:
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


# Cloud Run functions (2nd gen): the vCPUs that each memory size gets by
# default, from https://cloud.google.com/functions/docs/configuring/memory.
_GEN2_CPU = ((128, 0.083), (256, 0.167), (512, 0.333), (1024, 0.583), (2048, 1.0),
             (4096, 2.0), (8192, 2.0), (16384, 4.0), (32768, 8.0))
_MEMORY = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(k|ki|m|mi|g|gi)?\s*$", re.IGNORECASE)
_MEMORY_MB = {"": 1 / (1024 * 1024), "k": 1 / 1024, "ki": 1 / 1024, "m": 1, "mi": 1,
              "g": 1024, "gi": 1024}


def parse_memory_mb(value: Any) -> Optional[int]:
    """Megabytes from a memory size such as ``256M`` or ``1Gi``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str):
        return None
    match = _MEMORY.match(value)
    if not match:
        return None
    return round(float(match.group(1)) * _MEMORY_MB[(match.group(2) or "").lower()])


def _float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class CloudFunctionGen2(ComputeResource):
    """A Cloud Run function (2nd gen), which bills as a Cloud Run service (#375).

    GCP bills its requests, vCPU-seconds and GiB-seconds at the Cloud Run
    rates, so the quantities are priced from the Cloud Run rows and share
    their free tier.
    """

    @property
    def valid_metrics(self) -> list[str]:
        return ["invocations", "avgDurationMs", "memoryMb"]

    @property
    def catalog_services(self) -> dict[str, str]:
        return {metric: "CloudRun" for metric in
                ("CloudRun-Request", "CloudRun-vCPU-Second", "CloudRun-GiB-Second")}

    def derive_catalog_usage(self, usage: dict[str, float],
                             config: Optional[dict] = None) -> Optional[DerivedCatalogUsage]:
        """Requests, vCPU-seconds and GiB-seconds for each invocation.

        The duration rounds up to the next 100 ms. The vCPUs come from the
        node's ``config`` ``cpu``, or else from the memory size's default.
        """
        inputs = ("invocations", "avgDurationMs", "memoryMb")
        if not all(name in usage for name in inputs):
            return None
        invocations = usage["invocations"]
        memory_mb = usage["memoryMb"]
        cpu = _float((config or {}).get("cpu"))
        if cpu is None:
            cpu = next((vcpu for mb, vcpu in _GEN2_CPU if memory_mb <= mb), _GEN2_CPU[-1][1])
        seconds = max(math.ceil(usage["avgDurationMs"] / 100), 1) / 10
        return DerivedCatalogUsage(
            consumed=frozenset(inputs),
            quantities={
                "CloudRun-Request": invocations,
                "CloudRun-vCPU-Second": invocations * cpu * seconds,
                "CloudRun-GiB-Second": invocations * memory_mb / 1024 * seconds,
            },
        )

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["CloudFunctionGen2"]:
        if (resource_address.startswith("google_cloudfunctions2_function.") or
                "google:cloudfunctionsv2:Function:" in resource_address):
            return cls()
        return None

    @staticmethod
    def _extract(address: str, region: Any, service_config: dict, build_config: dict,
                 keys: tuple) -> ResourceExtract:
        memory_key, cpu_key, timeout_key = keys
        return ResourceExtract(
            resource_address=address,
            node_type="compute",
            provider="gcp",
            service="CloudFunctions",
            region=region,
            config={
                "generation": 2,
                "memoryMb": parse_memory_mb(service_config.get(memory_key)),
                "cpu": _float(service_config.get(cpu_key)),
                "timeout": service_config.get(timeout_key),
                "runtime": build_config.get("runtime"),
            },
        )

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})

        def block(name):
            value = values.get(name)
            if isinstance(value, list):
                value = value[0] if value else {}
            return value or {}

        return cls._extract(resource.get("address", ""), values.get("location"),
                            block("service_config"), block("build_config"),
                            ("available_memory", "available_cpu", "timeout_seconds"))

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        return cls._extract(resource.get("id", ""), inputs.get("location"),
                            inputs.get("serviceConfig") or {}, inputs.get("buildConfig") or {},
                            ("availableMemory", "availableCpu", "timeoutSeconds"))

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        raise NotImplementedError("CloudFormation has no Cloud Run functions")


# Cloud Storage classes with regional rows (#375). REGIONAL and
# MULTI_REGIONAL are the legacy names of Standard.
_GCS_CLASSES = {"standard": "Standard", "regional": "Standard", "multi_regional": "Standard",
                "nearline": "Nearline", "coldline": "Coldline", "archive": "Archive"}
_GCS_MULTI_REGIONS = {"us", "eu", "asia"}
_GCS_REGION = re.compile(r"^[a-z]+-[a-z]+\d+$")


def gcs_location_type(location: Any) -> str:
    """``region``, ``dual-region`` or ``multi-region`` for a bucket location.

    ``US``, ``EU`` and ``ASIA`` are multi-regions. A region has a name such
    as ``us-central1``. Any other location, such as ``NAM4``, is a
    dual-region. An unset location is the default, a region.
    """
    if not isinstance(location, str) or not location.strip():
        return "region"
    location = location.strip().lower()
    if location in _GCS_MULTI_REGIONS:
        return "multi-region"
    return "region" if _GCS_REGION.match(location) else "dual-region"


def gcs_storage_class(config: dict) -> str:
    """The storage class of a bucket's ``config``, as named in the metrics."""
    storage_class = config.get("storageClass")
    if not isinstance(storage_class, str) or not storage_class.strip():
        return "Standard"
    return _GCS_CLASSES.get(storage_class.strip().lower(), storage_class.strip())


def _lower(value: Any) -> Any:
    return value.lower() if isinstance(value, str) else value


class CloudStorage(StorageResource):
    """GCP Cloud Storage bucket - storage node (equivalent to AWS S3).

    The storage class and the location type select the rows (#375). The
    catalog has the rows of regional buckets. A dual-region or multi-region
    bucket gets metrics of its own, which have no rows yet, so the engine
    reports its usage as unpriced.
    """

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

    def catalog_metrics_for(self, config: dict) -> dict[str, str]:
        config = config or {}
        storage_class = gcs_storage_class(config)
        location_type = gcs_location_type(config.get("location"))
        if (storage_class, location_type) == ("Standard", "region"):
            return self.catalog_metrics
        prefix = f"GCS-{storage_class}"
        if location_type != "region":
            prefix += "-" + {"multi-region": "MultiRegion", "dual-region": "DualRegion"}[
                location_type]
        return {**self.catalog_metrics,
                "storageGb": f"{prefix}-GiB-Month",
                "writeRequests": f"{prefix}-Class-A-Operation",
                "readRequests": f"{prefix}-Class-B-Operation"}

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["CloudStorage"]:
        if (resource_address.startswith("google_storage_bucket.") or
                "google:storage:Bucket:" in resource_address or
                "Storage::Bucket:" in resource_address):
            return cls()
        return None

    @staticmethod
    def _extract(address: str, config: dict) -> ResourceExtract:
        location_type = gcs_location_type(config.get("location"))
        storage_class = gcs_storage_class(config)
        if location_type != "region":
            warnings.warn(
                f"{address}: a {location_type} bucket ({config.get('location')}) has no "
                f"catalog rows yet, so the engine reports its usage as unpriced."
            )
        if storage_class not in _GCS_CLASSES.values():
            warnings.warn(
                f"{address}: storage class {config.get('storageClass')!r} has no catalog "
                f"rows, so the engine reports its usage as unpriced."
            )
        return ResourceExtract(
            resource_address=address,
            node_type="storage",
            provider="gcp",
            service="CloudStorage",
            # The API gives a location in capitals, such as US-CENTRAL1.
            region=_lower(config.get("location")),
            config=config,
        )

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        versioning = values.get("versioning")
        if isinstance(versioning, list):
            versioning = versioning[0] if versioning else {}
        return cls._extract(resource.get("address", ""), {
            "location": values.get("location"),
            "storageClass": values.get("storage_class"),
            "versioning": (versioning or {}).get("enabled"),
        })

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        return cls._extract(resource.get("id", ""), {
            "location": inputs.get("location"),
            "storageClass": inputs.get("storageClass"),
        })

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
