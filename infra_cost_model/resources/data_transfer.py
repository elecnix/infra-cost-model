"""AWS Data Transfer usage node (Issue #211).

Data transfer is a usage-based cost, not something provisioned via
Terraform/Pulumi/CDK - it has no ``aws_*`` resource. It is therefore modeled
as a standalone external (leaf) node, conceptually like the external/LLM-token
nodes: a user attaches a named node with GB/month usage estimates.

Pricing dimensions (service ``AWSDataTransfer``, region ``us-east-1``):
- ``DataTransfer-InterRegion-GB``  - $0.02/GB (inter-region within North America)
- ``DataTransfer-Internet-Out-GB`` - $0.09/GB (internet egress, standard tier)
- ``DataTransfer-InterAZ-GB``      - $0.01/GB (regional, between AZs)

Ingress is free and has no paid metric.
"""

from typing import Optional

from infra_cost_model.pricing.catalog import PricingCatalog

from .types import ExternalResource, ResourceExtract

# Synthetic address prefixes. Data transfer has no real IaC resource, so the
# node is attached under one of these user-defined prefixes (mirroring the
# multi-format matching other handlers use).
_ADDRESS_PREFIXES = ("data_transfer.", "aws.datatransfer.", "aws:datatransfer:")


class DataTransferNode(ExternalResource):
    """Inter-region / internet data transfer node - a usage-derived leaf node.

    Not extracted from real infrastructure; matched by a synthetic address
    prefix so a user can attach GB/month usage estimates.
    """

    @property
    def valid_metrics(self) -> list[str]:
        return ["interRegionGb", "internetOutGb", "interAzGb"]

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["DataTransferNode"]:
        if resource_address.startswith(_ADDRESS_PREFIXES):
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="external", provider="aws", service="AWSDataTransfer",
            region=values.get("region"),
            config={
                "interRegionGb": values.get("inter_region_gb", 0),
                "internetOutGb": values.get("internet_out_gb", 0),
                "interAzGb": values.get("inter_az_gb", 0),
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="external", provider="aws", service="AWSDataTransfer",
            region=inputs.get("region"),
            config={
                "interRegionGb": inputs.get("interRegionGb", 0),
                "internetOutGb": inputs.get("internetOutGb", 0),
                "interAzGb": inputs.get("interAzGb", 0),
            },
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        properties = resource.get("Properties", {})
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="external", provider="aws", service="AWSDataTransfer",
            region=None,
            config={
                "interRegionGb": properties.get("InterRegionGb", 0),
                "internetOutGb": properties.get("InternetOutGb", 0),
                "interAzGb": properties.get("InterAzGb", 0),
            },
        )


def _data_transfer_cost(inter_region_gb=0, internet_out_gb=0, inter_az_gb=0, *,
                        catalog=None, provider: str = "aws",
                        region: str = "us-east-1") -> float:
    """Calculate monthly data transfer cost in USD.

    Args:
        inter_region_gb: GB transferred between regions (North America).
        internet_out_gb: GB egress to the internet (standard tier).
        inter_az_gb: GB transferred between AZs within a region.
        catalog: Optional PricingCatalog; created if not provided.
        provider: Cloud provider (default "aws").
        region: Region for the pricing lookup (default "us-east-1").

    Returns:
        Total monthly data transfer cost in USD. Ingress is free.
    """
    if catalog is None:
        catalog = PricingCatalog()
    total = 0.0
    dimensions = (
        (inter_region_gb, "DataTransfer-InterRegion-GB"),
        (internet_out_gb, "DataTransfer-Internet-Out-GB"),
        (inter_az_gb, "DataTransfer-InterAZ-GB"),
    )
    for quantity, metric in dimensions:
        if quantity > 0:
            r = catalog.query(provider, "AWSDataTransfer", region, metric, quantity)
            if r and hasattr(r, "total_cost"):
                total += r.total_cost
    return total
