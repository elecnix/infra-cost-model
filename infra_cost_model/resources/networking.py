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
from .types import RoutingResource, StorageResource, ResourceExtract


class NATGateway(RoutingResource):
    """NAT Gateway - routing node with always-on hourly + per-GB data cost."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["natHours", "dataProcessedGb"]

    @property
    def catalog_metrics(self) -> dict[str, str]:
        return {"natHours": "NAT-Gateway-Hour",
                "dataProcessedGb": "NAT-Gateway-DataProcessed"}

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["NATGateway"]:
        if (resource_address.startswith("aws_nat_gateway.") or
                resource_address.startswith("aws.nat.Gateway:") or
                resource_address.startswith("aws:ec2:NatGateway:") or
                "EC2::NatGateway:" in resource_address):
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

    @property
    def catalog_metrics(self) -> dict[str, str]:
        return {"endpointHours": "VPC-Endpoint-Hour",
                "dataProcessedGb": "VPC-Endpoint-DataProcessed"}

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["VpcEndpoint"]:
        if (resource_address.startswith("aws_vpc_endpoint.") or
                resource_address.startswith("aws.ec2.VpcEndpoint:") or
                resource_address.startswith("aws:ec2:VpcEndpoint:") or
                "EC2::VPCEndpoint:" in resource_address):
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


class ElasticIP(StorageResource):
    """Elastic IP / public IPv4 address - storage leaf node.

    Since February 2024, AWS charges $0.005/hr for every public IPv4 address,
    whether it is idle or attached to a running resource. Modeled as an
    always-on storage leaf (no outgoing edges).
    """

    @property
    def valid_metrics(self) -> list[str]:
        return ["inUseHours", "idleHours"]

    @property
    def catalog_metrics(self) -> dict[str, str]:
        return {"inUseHours": "IPv4-InUse-Hours", "idleHours": "IPv4-Idle-Hours"}

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["ElasticIP"]:
        if (resource_address.startswith("aws_eip.") or
                resource_address.startswith("aws.ec2.Eip:") or
                resource_address.startswith("aws:ec2:Eip:") or
                "EC2::EIP:" in resource_address):
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
                "domain": values.get("domain", "vpc"),
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
                "domain": inputs.get("domain", "vpc"),
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
                "domain": properties.get("Domain", "vpc"),
            },
        )
