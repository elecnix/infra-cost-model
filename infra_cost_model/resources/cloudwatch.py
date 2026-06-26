"""Amazon CloudWatch Logs resource model.

CloudWatch Logs is a storage (leaf) node with two cost dimensions:
- Log ingestion: $0.50 per GB ingested (the dominant term; scales with request volume)
- Log storage: $0.03 per GB-month (retained volume, driven by retention_in_days)

Actual AWS pricing includes a 5 GB per-account storage free tier. We model
conservatively without the free tier by default.
"""

from typing import Optional
from infra_cost_model.pricing.catalog import PricingCatalog
from .types import StorageResource, ResourceExtract


class CloudWatchLogGroup(StorageResource):
    """Amazon CloudWatch Log Group - storage node (leaf, no outgoing edges)."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["ingestedGb", "storedGb"]

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["CloudWatchLogGroup"]:
        if (resource_address.startswith("aws_cloudwatch_log_group.") or
                resource_address.startswith("aws.cloudwatch.LogGroup:") or
                resource_address.startswith("aws:cloudwatch:LogGroup:") or
                "Logs::LogGroup" in resource_address):
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="storage", provider="aws", service="AmazonCloudWatch",
            region=values.get("region"),
            config={
                "name": values.get("name"),
                "retentionInDays": values.get("retention_in_days", 0),
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="storage", provider="aws", service="AmazonCloudWatch",
            region=inputs.get("region"),
            config={
                "name": inputs.get("name"),
                "retentionInDays": inputs.get("retentionInDays", 0),
            },
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        properties = resource.get("Properties", {})
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="storage", provider="aws", service="AmazonCloudWatch",
            region=None,
            config={
                "name": properties.get("LogGroupName"),
                "retentionInDays": properties.get("RetentionInDays", 0),
            },
        )


def _cloudwatch_log_cost(ingested_gb=0.0, stored_gb=0.0, *,
                         catalog=None, provider: str = "aws",
                         region: str = "us-east-1") -> float:
    if catalog is None:
        catalog = PricingCatalog()
    total = 0.0
    if ingested_gb > 0:
        r = catalog.query(provider, "AmazonCloudWatch", region,
                          "CloudWatch-Log-Ingestion", ingested_gb)
        if r and hasattr(r, "total_cost"):
            total += r.total_cost
    if stored_gb > 0:
        r = catalog.query(provider, "AmazonCloudWatch", region,
                          "CloudWatch-Log-Storage", stored_gb)
        if r and hasattr(r, "total_cost"):
            total += r.total_cost
    return total
