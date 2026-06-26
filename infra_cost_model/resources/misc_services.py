"""AWS Secrets Manager, ECR, and Route53 resource models.

Three small recurring-cost AWS services grouped for minimal per-handler overhead.
- SecretsManagerSecret: $/secret-month + $/10K API calls
- ECRRepository: $/GB-month storage
- Route53Zone: $/hosted-zone-month + $/M queries
"""

from typing import Optional
from infra_cost_model.pricing.catalog import PricingCatalog
from .types import StorageResource, ResourceExtract


class SecretsManagerSecret(StorageResource):
    """AWS Secrets Manager Secret - storage node for secrets management."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["secretsCount", "apiCalls"]

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["SecretsManagerSecret"]:
        if (resource_address.startswith("aws_secretsmanager_secret.") or
                resource_address.startswith("aws.secretsmanager.Secret:") or
                "SecretsManager::Secret" in resource_address):
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        rotation = values.get("rotation", {})
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="storage", provider="aws", service="AWSSecretsManager",
            region=values.get("region"),
            config={
                "name": values.get("name"),
                "rotationDays":
                    rotation.get("rotation_days")
                    if isinstance(rotation, dict) else None,
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        rotation = inputs.get("rotation", {})
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="storage", provider="aws", service="AWSSecretsManager",
            region=inputs.get("region"),
            config={
                "name": inputs.get("name"),
                "rotationDays":
                    rotation.get("rotationDays")
                    if isinstance(rotation, dict) else None,
            },
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        properties = resource.get("Properties", {})
        rotation = properties.get("RotationSchedule", {}) or {}
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="storage", provider="aws", service="AWSSecretsManager",
            region=None,
            config={
                "name": properties.get("Name"),
                "rotationDays":
                    rotation.get("RotationDays")
                    if isinstance(rotation, dict) else None,
            },
        )


class ECRRepository(StorageResource):
    """Amazon ECR Repository - storage node for container image storage."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["storedGb"]

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["ECRRepository"]:
        if (resource_address.startswith("aws_ecr_repository.") or
                resource_address.startswith("aws.ecr.Repository:") or
                "ECR::Repository" in resource_address):
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="storage", provider="aws", service="AmazonECR",
            region=values.get("region"),
            config={
                "name": values.get("name"),
                "imageTagMutability": values.get("image_tag_mutability", "MUTABLE"),
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="storage", provider="aws", service="AmazonECR",
            region=inputs.get("region"),
            config={
                "name": inputs.get("name"),
                "imageTagMutability": inputs.get("imageTagMutability", "MUTABLE"),
            },
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        properties = resource.get("Properties", {})
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="storage", provider="aws", service="AmazonECR",
            region=None,
            config={
                "name": properties.get("RepositoryName"),
                "imageTagMutability": properties.get("ImageTagMutability", "MUTABLE"),
            },
        )


class Route53Zone(StorageResource):
    """Amazon Route53 Hosted Zone - storage node for DNS hosting."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["hostedZones", "queries"]

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["Route53Zone"]:
        if (resource_address.startswith("aws_route53_zone.") or
                resource_address.startswith("aws.route53.Zone:") or
                "Route53::HostedZone" in resource_address):
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="storage", provider="aws", service="AmazonRoute53",
            region=values.get("region"),
            config={
                "name": values.get("name"),
                "comment": values.get("comment"),
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="storage", provider="aws", service="AmazonRoute53",
            region=inputs.get("region"),
            config={
                "name": inputs.get("name"),
                "comment": inputs.get("comment"),
            },
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        properties = resource.get("Properties", {})
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="storage", provider="aws", service="AmazonRoute53",
            region=None,
            config={
                "name": properties.get("Name"),
                "comment": properties.get("HostedZoneConfig", {}).get("Comment"),
            },
        )


def _secretsmanager_cost(secrets_count=1, api_calls=0, *,
                         catalog=None, provider: str = "aws", region: str) -> float:
    if catalog is None:
        catalog = PricingCatalog()
    total = 0.0
    if secrets_count > 0:
        r = catalog.query(provider, "AWSSecretsManager", region,
                          "SecretsManager-Secret", secrets_count)
        if r and hasattr(r, "total_cost"):
            total += r.total_cost
    if api_calls > 0:
        r = catalog.query(provider, "AWSSecretsManager", region,
                          "SecretsManager-API-Call", api_calls)
        if r and hasattr(r, "total_cost"):
            total += r.total_cost
    return total


def _ecr_cost(stored_gb=1, *, catalog=None,
              provider: str = "aws", region: str) -> float:
    if catalog is None:
        catalog = PricingCatalog()
    total = 0.0
    if stored_gb > 0:
        r = catalog.query(provider, "AmazonECR", region,
                          "ECR-Storage", stored_gb)
        if r and hasattr(r, "total_cost"):
            total += r.total_cost
    return total


def _route53_cost(hosted_zones=1, queries=0, *,
                  catalog=None, provider: str = "aws", region: str) -> float:
    if catalog is None:
        catalog = PricingCatalog()
    total = 0.0
    if hosted_zones > 0:
        r = catalog.query(provider, "AmazonRoute53", region,
                          "Route53-HostedZone", hosted_zones)
        if r and hasattr(r, "total_cost"):
            total += r.total_cost
    if queries > 0:
        r = catalog.query(provider, "AmazonRoute53", region,
                          "Route53-Query", queries)
        if r and hasattr(r, "total_cost"):
            total += r.total_cost
    return total
