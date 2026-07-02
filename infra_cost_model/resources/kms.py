"""AWS KMS key resource model.

Native handler for AWS KMS customer-managed keys.
- Recurring cost: $/customer-managed-key-month
- Usage cost: $/symmetric-API-request (with a 20,000-request free tier)
"""

from typing import Optional
from infra_cost_model.pricing.catalog import PricingCatalog
from .types import StorageResource, ResourceExtract


class KMSKey(StorageResource):
    """AWS KMS Key - storage node for a customer-managed encryption key."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["keysCount", "apiRequests"]

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["KMSKey"]:
        if (resource_address.startswith("aws_kms_key.") or
                resource_address.startswith("aws.kms.Key:") or
                "KMS::Key" in resource_address):
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="storage", provider="aws", service="AWSKMS",
            region=values.get("region"),
            config={
                "description": values.get("description"),
                "keyUsage": values.get("key_usage", "ENCRYPT_DECRYPT"),
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="storage", provider="aws", service="AWSKMS",
            region=inputs.get("region"),
            config={
                "description": inputs.get("description"),
                "keyUsage": inputs.get("keyUsage", "ENCRYPT_DECRYPT"),
            },
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        properties = resource.get("Properties", {})
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="storage", provider="aws", service="AWSKMS",
            region=None,
            config={
                "description": properties.get("Description"),
                "keyUsage": properties.get("KeyUsage", "ENCRYPT_DECRYPT"),
            },
        )


def _kms_cost(keys_count=1, api_requests=0, *,
              catalog=None, provider: str = "aws", region: str) -> float:
    if catalog is None:
        catalog = PricingCatalog()
    total = 0.0
    if keys_count > 0:
        r = catalog.query(provider, "AWSKMS", region,
                          "KMS-Key-Month", keys_count)
        if r and hasattr(r, "total_cost"):
            total += r.total_cost
    if api_requests > 0:
        r = catalog.query(provider, "AWSKMS", region,
                          "KMS-API-Request", api_requests)
        if r and hasattr(r, "total_cost"):
            total += r.total_cost
    return total
