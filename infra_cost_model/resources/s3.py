"""Amazon S3 Bucket resource model.

S3 is the foundational storage resource in the AWS ecosystem.
Pricing covers 4 dimensions with tiered rates:
- PUT/COPY/POST/LIST requests: $0.005/1K
- GET requests: $0.0004/1K
- Storage: $0.023/GB-month (first 50TB)
- Data transfer out to internet: $0.09/GB (first 10TB)
"""

from typing import Optional

from infra_cost_model.pricing.catalog import PricingCatalog

from .types import StorageResource, ResourceExtract


class S3Bucket(StorageResource):
    """Amazon S3 Bucket - storage node (leaf, no outgoing edges)."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["putRequests", "getRequests", "storageGb", "dataOutGb"]

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["S3Bucket"]:
        """Parse resource address to determine if it's an S3 bucket."""
        if (resource_address.startswith("aws_s3_bucket.") or
                resource_address.startswith("aws.s3.Bucket:") or
                resource_address.startswith("aws:s3:Bucket:") or
                "S3::Bucket" in resource_address):
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        """Extract from Terraform aws_s3_bucket resource."""
        values = resource.get("values", {})

        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="storage",
            provider="aws",
            service="AmazonS3",
            region=values.get("region"),
            config={
                "bucket": values.get("bucket"),
                "acl": values.get("acl"),
                "versioning": values.get("versioning", {}).get("enabled")
                if isinstance(values.get("versioning"), dict) else None,
                "lifecycleRules": cls._parse_lifecycle_rules(
                    values.get("lifecycle_rule", [])
                ),
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        """Extract from Pulumi aws.s3.Bucket resource."""
        inputs = resource.get("inputs", {})

        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="storage",
            provider="aws",
            service="AmazonS3",
            region=inputs.get("region"),
            config={
                "bucket": inputs.get("bucket"),
                "acl": inputs.get("acl"),
                "versioning": inputs.get("versioning", {}).get("enabled")
                if isinstance(inputs.get("versioning"), dict) else None,
            },
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        """Extract from CDK CloudFormation S3::Bucket."""
        properties = resource.get("Properties", {})

        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="storage",
            provider="aws",
            service="AmazonS3",
            region=None,
            config={
                "bucket": properties.get("BucketName"),
                "acl": properties.get("AccessControl"),
                "versioning": properties.get(
                    "VersioningConfiguration", {}
                ).get("Status") == "Enabled",
                "removalPolicy": properties.get("DeletionPolicy"),
                "encryption": properties.get(
                    "BucketEncryption", {}
                ).get("ServerSideEncryptionConfiguration"),
            },
        )

    @staticmethod
    def _parse_lifecycle_rules(rules: list[dict]) -> list[dict]:
        """Parse Terraform lifecycle rules into simplified structure."""
        parsed = []
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            entry: dict[str, object] = {"id": rule.get("id", "")}
            if rule.get("enabled"):
                entry["enabled"] = True
            expiration = rule.get("expiration", {})
            if isinstance(expiration, dict) and expiration.get("days"):
                entry["expirationDays"] = expiration["days"]
            transition = rule.get("transition", [])
            if isinstance(transition, dict):
                transition = [transition]
            transitions = []
            for t in transition:
                if isinstance(t, dict):
                    transitions.append({
                        "days": t.get("days"),
                        "storageClass": t.get("storage_class"),
                    })
            if transitions:
                entry["transitions"] = transitions
            parsed.append(entry)
        return parsed


def _s3_cost(
    put_requests: float = 0,
    get_requests: float = 0,
    storage_gb: float = 0,
    data_out_gb: float = 0,
    catalog=None,
    region: str = "us-east-1",
) -> float:
    """Calculate S3 cost using catalog pricing.

    Args:
        put_requests: Monthly PUT/COPY/POST/LIST requests
        get_requests: Monthly GET requests
        storage_gb: Data stored in GB-month
        data_out_gb: Monthly data transferred out to internet
        catalog: Optional PricingCatalog (uses default if None)
        region: AWS region for pricing lookup

    Returns:
        Total monthly cost in USD.
    """
    if catalog is None:
        catalog = PricingCatalog()

    total = 0.0

    # PUT requests: $0.005/1K
    if put_requests > 0:
        result = catalog.query("aws", "AmazonS3", region,
                               "S3-PutRequest", put_requests)
        if result and hasattr(result, "total_cost"):
            total += result.total_cost

    # GET requests: $0.0004/1K
    if get_requests > 0:
        result = catalog.query("aws", "AmazonS3", region,
                               "S3-GetRequest", get_requests)
        if result and hasattr(result, "total_cost"):
            total += result.total_cost

    # Storage: tiered pricing (first 50TB at $0.023/GB)
    if storage_gb > 0:
        result = catalog.query("aws", "AmazonS3", region,
                               "S3-Storage", storage_gb)
        if result and hasattr(result, "total_cost"):
            total += result.total_cost

    # Data transfer: tiered pricing (first 10TB at $0.09/GB)
    if data_out_gb > 0:
        result = catalog.query("aws", "AmazonS3", region,
                               "S3-DataTransfer", data_out_gb)
        if result and hasattr(result, "total_cost"):
            total += result.total_cost

    return total
