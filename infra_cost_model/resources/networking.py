"""NAT Gateway and VPC Endpoint resource models.

NAT Gateway (always-on routing node) and VPC Interface Endpoint (always-on
storage node). Both carry recurring hourly + per-GB costs that often rival
compute on low-traffic stacks.

Pricing:
- NAT Gateway: $0.045/hr + $0.045/GB processed
- VPC Interface Endpoint: $0.01/ENI-hour + $0.01/GB processed
- VPC Gateway Endpoint (S3/DynamoDB): free
"""

from typing import Optional
from infra_cost_model.pricing.catalog import PricingCatalog
from .types import RoutingResource, StorageResource, ResourceExtract


class NATGateway(RoutingResource):
    """NAT Gateway - routing node with always-on hourly + per-GB data cost."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["natHours", "dataProcessedGb"]

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["NATGateway"]:
        if (resource_address.startswith("aws_nat_gateway.") or
                resource_address.startswith("aws.nat.Gateway:") or
                resource_address.startswith("aws:ec2:NatGateway:") or
                "EC2::NatGateway" in resource_address):
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="routing", provider="aws", service="AmazonVPC",
            region=values.get("region"),
            config={
                "connectivityType": values.get("connectivity_type", "public"),
                "subnetId": values.get("subnet_id"),
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="routing", provider="aws", service="AmazonVPC",
            region=inputs.get("region"),
            config={
                "connectivityType": inputs.get("connectivityType", "public"),
                "subnetId": inputs.get("subnetId"),
            },
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        properties = resource.get("Properties", {})
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="routing", provider="aws", service="AmazonVPC",
            region=None,
            config={
                "connectivityType": properties.get("ConnectivityType", "public"),
                "subnetId": properties.get("SubnetId"),
            },
        )


class VpcEndpoint(StorageResource):
    """VPC Endpoint - storage leaf node.

    Gateway endpoints (S3, DynamoDB) are free.
    Interface endpoints cost per ENI-hour + per-GB processed.
    """

    @property
    def valid_metrics(self) -> list[str]:
        return ["endpointHours", "dataProcessedGb"]

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["VpcEndpoint"]:
        if (resource_address.startswith("aws_vpc_endpoint.") or
                resource_address.startswith("aws.ec2.VpcEndpoint:") or
                resource_address.startswith("aws:ec2:VpcEndpoint:") or
                "EC2::VPCEndpoint" in resource_address):
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="storage", provider="aws", service="AmazonVPC",
            region=values.get("region"),
            config={
                "serviceName": values.get("service_name", ""),
                "vpcEndpointType": values.get("vpc_endpoint_type", "Gateway"),
                "subnetIds": values.get("subnet_ids", []),
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="storage", provider="aws", service="AmazonVPC",
            region=inputs.get("region"),
            config={
                "serviceName": inputs.get("serviceName", ""),
                "vpcEndpointType": inputs.get("vpcEndpointType", "Gateway"),
                "subnetIds": inputs.get("subnetIds", []),
            },
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        properties = resource.get("Properties", {})
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="storage", provider="aws", service="AmazonVPC",
            region=None,
            config={
                "serviceName": properties.get("ServiceName", ""),
                "vpcEndpointType": properties.get("VpcEndpointType", "Gateway"),
                "subnetIds": properties.get("SubnetIds", []),
            },
        )


def _nat_cost(nat_hours=730, data_processed_gb=0, *, catalog=None, provider: str = "aws", region: str = "us-east-1") -> float:
    """Calculate NAT Gateway cost.

    Args:
        nat_hours: Hours of NAT gateway runtime (default 730 = 1 month)
        data_processed_gb: GB of data processed through the NAT gateway
    """
    if catalog is None:
        catalog = PricingCatalog()
    total = 0.0
    if nat_hours > 0:
        r = catalog.query(provider, "AmazonVPC", region, "NAT-Gateway-Hour", nat_hours)
        if r and hasattr(r, "total_cost"):
            total += r.total_cost
    if data_processed_gb > 0:
        r = catalog.query(provider, "AmazonVPC", region, "NAT-Gateway-DataProcessed", data_processed_gb)
        if r and hasattr(r, "total_cost"):
            total += r.total_cost
    return total


def _vpc_endpoint_cost(endpoint_hours=730, data_processed_gb=0, endpoint_type="Interface",
                       subnet_count=1, *, catalog=None, provider: str = "aws", region: str = "us-east-1") -> float:
    """Calculate VPC Endpoint cost.

    Gateway endpoints (S3, DynamoDB) are free.
    Interface endpoints cost per ENI-hour × subnet_count + per-GB processed.

    Args:
        endpoint_hours: Hours of endpoint runtime (default 730)
        data_processed_gb: GB of data processed through the endpoint
        endpoint_type: "Gateway" or "Interface"
        subnet_count: Number of subnets/ENIs (for Interface endpoints)
    """
    if endpoint_type == "Gateway":
        return 0.0

    if catalog is None:
        catalog = PricingCatalog()
    total = 0.0
    if endpoint_hours > 0:
        # Interface endpoint: hourly cost scales with ENI count (one per subnet)
        total_hours = endpoint_hours * subnet_count
        r = catalog.query(provider, "AmazonVPC", region, "VPC-Endpoint-Hour", total_hours)
        if r and hasattr(r, "total_cost"):
            total += r.total_cost
    if data_processed_gb > 0:
        r = catalog.query(provider, "AmazonVPC", region, "VPC-Endpoint-DataProcessed", data_processed_gb)
        if r and hasattr(r, "total_cost"):
            total += r.total_cost
    return total
