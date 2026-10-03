"""Tests for NAT Gateway and VPC Endpoint resource models (Issue #184)."""
import pytest
from functools import partial

from infra_cost_model.resources.networking import (
    NATGateway, VpcEndpoint, ElasticIP,
)
from infra_cost_model.pricing.catalog import PricingCatalog
from live_pricing import resource_cost

_NAT = partial(resource_cost, "aws_nat_gateway.main", "AmazonVPC", "us-east-1")
_VPC = partial(resource_cost, "aws_vpc_endpoint.main", "AmazonVPC", "us-east-1")
_EIP = partial(resource_cost, "aws_eip.main", "AmazonVPC", "us-east-1")


class TestNATAddressParsing:
    def test_from_address_terraform(self):
        r = NATGateway.from_address("aws_nat_gateway.main")
        assert r is not None and r.node_type == "routing"

    def test_from_address_pulumi(self):
        r = NATGateway.from_address("aws.nat.Gateway:main-nat")
        assert r is not None and r.node_type == "routing"

    def test_from_address_cdk(self):
        # CDK synthetic address format built by extract_resources_from_cdk: "<Type>:<LogicalId>"
        r = NATGateway.from_address("AWS::EC2::NatGateway:MainNat")
        assert r is not None and r.node_type == "routing"

    def test_from_address_aws_format(self):
        assert NATGateway.from_address("aws:ec2:NatGateway:prod-nat") is not None

    def test_from_address_unrelated(self):
        assert NATGateway.from_address("aws_lambda_function.handler") is None


class TestNATExtraction:
    def test_extract_tf(self):
        resource = {
            "address": "aws_nat_gateway.main",
            "type": "aws_nat_gateway",
            "values": {
                "connectivity_type": "public",
                "subnet_id": "subnet-abc123",
                "region": "us-east-1",
            },
        }
        result = NATGateway.extract_tf(resource)
        assert result.node_type == "routing" and result.provider == "aws" and result.service == "AmazonVPC"
        assert result.config["connectivityType"] == "public"
        assert result.config["subnetId"] == "subnet-abc123"

    def test_extract_tf_defaults(self):
        resource = {"address": "aws_nat_gateway.backup", "type": "aws_nat_gateway", "values": {}}
        result = NATGateway.extract_tf(resource)
        assert result.config["connectivityType"] == "public"

    def test_extract_pulumi(self):
        resource = {
            "id": "aws.nat.Gateway:prod-nat",
            "type": "aws.nat.Gateway",
            "inputs": {"connectivityType": "public", "subnetId": "subnet-xyz", "region": "us-west-2"},
        }
        result = NATGateway.extract_pulumi(resource)
        assert result.provider == "aws"
        assert result.config["connectivityType"] == "public"
        assert result.config["subnetId"] == "subnet-xyz"

    def test_extract_cdk(self):
        resource = {
            "Type": "AWS::EC2::NatGateway",
            "LogicalId": "MainNatGateway",
            "Properties": {"ConnectivityType": "public", "SubnetId": "subnet-001"},
        }
        result = NATGateway.extract_cdk(resource)
        assert result.config["connectivityType"] == "public"
        assert result.config["subnetId"] == "subnet-001"


class TestNATPricing:
    def setup_method(self):
        self.catalog = PricingCatalog(seed=True)
        self.nat = partial(_NAT, catalog=self.catalog)

    def test_hours_only(self):
        cost = self.nat(natHours=730, dataProcessedGb=0)
        assert cost == pytest.approx(32.85, rel=0.01)

    def test_data_processed_only(self):
        cost = self.nat(natHours=0, dataProcessedGb=100)
        assert cost == pytest.approx(4.50, rel=0.01)

    def test_combined(self):
        cost = self.nat(natHours=730, dataProcessedGb=100)
        assert cost == pytest.approx(37.35, rel=0.01)

    def test_zero_usage(self):
        assert self.nat(natHours=0, dataProcessedGb=0) == 0.0


class TestNATNodeType:
    def test_nat_is_routing_node(self):
        result = NATGateway.from_address("aws_nat_gateway.test")
        assert result is not None and result.node_type == "routing"

    def test_nat_is_not_leaf(self):
        from infra_cost_model.resources.registry import is_leaf_node
        assert is_leaf_node("routing") is False

    def test_nat_valid_metrics(self):
        n = NATGateway()
        assert "natHours" in n.valid_metrics
        assert "dataProcessedGb" in n.valid_metrics


class TestVPCEndpointAddressParsing:
    def test_from_address_terraform(self):
        r = VpcEndpoint.from_address("aws_vpc_endpoint.s3")
        assert r is not None and r.node_type == "storage"

    def test_from_address_pulumi(self):
        r = VpcEndpoint.from_address("aws.ec2.VpcEndpoint:s3-endpoint")
        assert r is not None and r.node_type == "storage"

    def test_from_address_cdk(self):
        r = VpcEndpoint.from_address("AWS::EC2::VPCEndpoint:S3Endpoint")
        assert r is not None and r.node_type == "storage"

    def test_from_address_aws_format(self):
        assert VpcEndpoint.from_address("aws:ec2:VpcEndpoint:s3-vpce") is not None

    def test_from_address_does_not_match_endpoint_service(self):
        # AWS::EC2::VPCEndpointService / VPCEndpointConnectionNotification are
        # distinct CFN types and must not be mis-matched as a VPC Endpoint.
        assert VpcEndpoint.from_address("AWS::EC2::VPCEndpointService:MySvc") is None
        assert VpcEndpoint.from_address(
            "AWS::EC2::VPCEndpointConnectionNotification:MyNotif") is None

    def test_from_address_unrelated(self):
        assert VpcEndpoint.from_address("aws_lambda_function.handler") is None


class TestVPCEndpointExtraction:
    def test_extract_tf_gateway(self):
        resource = {
            "address": "aws_vpc_endpoint.s3",
            "type": "aws_vpc_endpoint",
            "values": {
                "service_name": "com.amazonaws.us-east-1.s3",
                "vpc_endpoint_type": "Gateway",
                "subnet_ids": [],
                "region": "us-east-1",
            },
        }
        result = VpcEndpoint.extract_tf(resource)
        assert result.node_type == "storage" and result.provider == "aws" and result.service == "AmazonVPC"
        assert result.config["vpcEndpointType"] == "Gateway"
        assert result.config["serviceName"] == "com.amazonaws.us-east-1.s3"
        assert result.config["subnetIds"] == []

    def test_extract_tf_interface(self):
        resource = {
            "address": "aws_vpc_endpoint.ecr",
            "type": "aws_vpc_endpoint",
            "values": {
                "service_name": "com.amazonaws.us-east-1.ecr.dkr",
                "vpc_endpoint_type": "Interface",
                "subnet_ids": ["subnet-a", "subnet-b"],
                "region": "us-east-1",
            },
        }
        result = VpcEndpoint.extract_tf(resource)
        assert result.config["vpcEndpointType"] == "Interface"
        assert result.config["subnetIds"] == ["subnet-a", "subnet-b"]

    def test_extract_tf_defaults(self):
        resource = {"address": "aws_vpc_endpoint.generic", "type": "aws_vpc_endpoint", "values": {}}
        result = VpcEndpoint.extract_tf(resource)
        assert result.config["vpcEndpointType"] == "Gateway"
        assert result.config["serviceName"] == ""
        assert result.config["subnetIds"] == []

    def test_extract_pulumi(self):
        resource = {
            "id": "aws.ec2.VpcEndpoint:secrets",
            "type": "aws.ec2.VpcEndpoint",
            "inputs": {
                "serviceName": "com.amazonaws.us-east-1.secretsmanager",
                "vpcEndpointType": "Interface",
                "subnetIds": ["subnet-1"],
                "region": "us-west-2",
            },
        }
        result = VpcEndpoint.extract_pulumi(resource)
        assert result.config["vpcEndpointType"] == "Interface"
        assert result.config["subnetIds"] == ["subnet-1"]

    def test_extract_cdk(self):
        resource = {
            "Type": "AWS::EC2::VPCEndpoint",
            "LogicalId": "DynamoEndpoint",
            "Properties": {
                "ServiceName": "com.amazonaws.us-east-1.dynamodb",
                "VpcEndpointType": "Gateway",
                "SubnetIds": [],
            },
        }
        result = VpcEndpoint.extract_cdk(resource)
        assert result.config["vpcEndpointType"] == "Gateway"
        assert result.config["serviceName"] == "com.amazonaws.us-east-1.dynamodb"


class TestVPCEndpointPricing:
    def setup_method(self):
        self.catalog = PricingCatalog(seed=True)
        self.vpc = partial(_VPC, catalog=self.catalog)

    def test_gateway_is_free(self):
        # A gateway endpoint consumes no endpoint-hours and no data
        # processing, so it bills nothing.
        assert self.vpc(endpointHours=0, dataProcessedGb=0) == 0.0

    def test_interface_hours_single_subnet(self):
        cost = self.vpc(endpointHours=730 * 1, dataProcessedGb=0)
        assert cost == pytest.approx(7.30, rel=0.01)

    def test_interface_hours_two_subnets(self):
        # 730 * 2 subnets = 1460 ENI-hours
        cost = self.vpc(endpointHours=730 * 2, dataProcessedGb=0)
        assert cost == pytest.approx(14.60, rel=0.01)

    def test_interface_data_processed(self):
        cost = self.vpc(endpointHours=0, dataProcessedGb=100)
        assert cost == pytest.approx(1.00, rel=0.01)

    def test_interface_combined(self):
        # 730*2 hours at $0.01 = $14.60 + 100 GB at $0.01 = $1.00
        cost = self.vpc(endpointHours=730 * 2, dataProcessedGb=100)
        assert cost == pytest.approx(15.60, rel=0.01)

    def test_zero_usage_interface(self):
        assert self.vpc(endpointHours=0, dataProcessedGb=0) == 0.0


class TestVPCEndpointNodeType:
    def test_vpc_endpoint_is_storage_leaf(self):
        result = VpcEndpoint.from_address("aws_vpc_endpoint.test")
        assert result is not None and result.node_type == "storage"
        from infra_cost_model.resources.registry import is_leaf_node
        assert is_leaf_node(result.node_type) is True

    def test_vpc_endpoint_valid_metrics(self):
        v = VpcEndpoint()
        assert "endpointHours" in v.valid_metrics
        assert "dataProcessedGb" in v.valid_metrics


class TestNetworkingRegistry:
    def test_nat_in_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        assert ResourceRegistry.from_address("aws_nat_gateway.main") == NATGateway

    def test_vpc_endpoint_in_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        assert ResourceRegistry.from_address("aws_vpc_endpoint.s3") == VpcEndpoint

    def test_nat_extract_via_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        resource = {
            "address": "aws_nat_gateway.main",
            "type": "aws_nat_gateway",
            "values": {"connectivity_type": "public", "subnet_id": "subnet-abc", "region": "us-east-1"},
        }
        result = ResourceRegistry.extract("aws_nat_gateway.main", resource, "terraform")
        assert result is not None and result["provider"] == "aws" and result["service"] == "AmazonVPC"
        assert result["nodeType"] == "routing"

    def test_vpc_endpoint_extract_via_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        resource = {
            "address": "aws_vpc_endpoint.ecr",
            "type": "aws_vpc_endpoint",
            "values": {
                "service_name": "com.amazonaws.us-east-1.ecr.dkr",
                "vpc_endpoint_type": "Interface",
                "subnet_ids": ["subnet-a"],
                "region": "us-east-1",
            },
        }
        result = ResourceRegistry.extract("aws_vpc_endpoint.ecr", resource, "terraform")
        assert result is not None and result["provider"] == "aws" and result["service"] == "AmazonVPC"
        assert result["nodeType"] == "storage"
        assert result["config"]["vpcEndpointType"] == "Interface"


class TestElasticIPAddressParsing:
    def test_from_address_terraform(self):
        r = ElasticIP.from_address("aws_eip.nat")
        assert r is not None and r.node_type == "storage"

    def test_from_address_pulumi_dot(self):
        r = ElasticIP.from_address("aws.ec2.Eip:web-ip")
        assert r is not None and r.node_type == "storage"

    def test_from_address_pulumi_colon(self):
        r = ElasticIP.from_address("aws:ec2:Eip:prod-ip")
        assert r is not None and r.node_type == "storage"

    def test_from_address_cdk(self):
        r = ElasticIP.from_address("AWS::EC2::EIP:NatEip")
        assert r is not None and r.node_type == "storage"

    def test_from_address_unrelated(self):
        assert ElasticIP.from_address("aws_lambda_function.handler") is None
        # Ensure it does not greedily match unrelated eip-like prefixes
        assert ElasticIP.from_address("aws_eip_association.assoc") is None
        # CDK: AWS::EC2::EIPAssociation is a distinct type and must not match.
        assert ElasticIP.from_address("AWS::EC2::EIPAssociation:MyAssoc") is None


class TestElasticIPExtraction:
    def test_extract_tf(self):
        resource = {
            "address": "aws_eip.nat",
            "type": "aws_eip",
            "values": {"domain": "vpc", "region": "us-east-1"},
        }
        result = ElasticIP.extract_tf(resource)
        assert result.node_type == "storage" and result.provider == "aws" and result.service == "AmazonVPC"
        assert result.resource_address == "aws_eip.nat"
        assert result.region == "us-east-1"
        assert result.config["domain"] == "vpc"

    def test_extract_tf_defaults(self):
        resource = {"address": "aws_eip.generic", "type": "aws_eip", "values": {}}
        result = ElasticIP.extract_tf(resource)
        assert result.node_type == "storage"
        assert result.config["domain"] == "vpc"
        assert result.region is None

    def test_extract_pulumi(self):
        resource = {
            "id": "aws:ec2:Eip:prod-ip",
            "type": "aws:ec2:Eip",
            "inputs": {"domain": "vpc", "region": "us-west-2"},
        }
        result = ElasticIP.extract_pulumi(resource)
        assert result.provider == "aws" and result.service == "AmazonVPC"
        assert result.region == "us-west-2"
        assert result.config["domain"] == "vpc"

    def test_extract_cdk(self):
        resource = {
            "Type": "AWS::EC2::EIP",
            "LogicalId": "NatEip",
            "Properties": {"Domain": "vpc"},
        }
        result = ElasticIP.extract_cdk(resource)
        assert result.node_type == "storage" and result.service == "AmazonVPC"
        assert result.resource_address == "NatEip"
        assert result.config["domain"] == "vpc"


class TestElasticIPPricing:
    def setup_method(self):
        self.catalog = PricingCatalog(seed=True)
        self.eip = partial(_EIP, catalog=self.catalog)

    def test_in_use_hours(self):
        cost = self.eip(inUseHours=730, idleHours=0)
        assert cost == pytest.approx(3.65, rel=0.01)

    def test_idle_hours(self):
        cost = self.eip(inUseHours=0, idleHours=730)
        assert cost == pytest.approx(3.65, rel=0.01)

    def test_combined(self):
        cost = self.eip(inUseHours=730, idleHours=730)
        assert cost == pytest.approx(7.30, rel=0.01)

    def test_zero_usage(self):
        assert self.eip(inUseHours=0, idleHours=0) == 0.0

    def test_one_always_on_public_ipv4(self):
        # An address in use for a whole month bills 730 in-use hours.
        cost = self.eip(inUseHours=730, idleHours=0)
        assert cost == pytest.approx(3.65, rel=0.01)


class TestElasticIPNodeType:
    def test_eip_is_storage_leaf(self):
        result = ElasticIP.from_address("aws_eip.test")
        assert result is not None and result.node_type == "storage"
        from infra_cost_model.resources.registry import is_leaf_node
        assert is_leaf_node(result.node_type) is True

    def test_eip_valid_metrics(self):
        e = ElasticIP()
        assert "inUseHours" in e.valid_metrics
        assert "idleHours" in e.valid_metrics


class TestElasticIPRegistry:
    def test_eip_in_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        assert ResourceRegistry.from_address("aws_eip.nat") == ElasticIP

    def test_eip_extract_via_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        resource = {
            "address": "aws_eip.nat",
            "type": "aws_eip",
            "values": {"domain": "vpc", "region": "us-east-1"},
        }
        result = ResourceRegistry.extract("aws_eip.nat", resource, "terraform")
        assert result is not None and result["provider"] == "aws" and result["service"] == "AmazonVPC"
        assert result["nodeType"] == "storage"
        assert result["config"]["domain"] == "vpc"
