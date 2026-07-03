"""Tests for the Data Transfer usage node (Issue #211).

Data transfer is a usage-based cost with no Terraform/Pulumi/CDK resource.
It is modeled as a standalone external (leaf) node that a user attaches with
GB/month usage estimates, analogous to the external/LLM-token nodes.
"""

import pytest

from infra_cost_model.resources.data_transfer import (
    DataTransferNode,
    _data_transfer_cost,
)
from infra_cost_model.pricing.catalog import PricingCatalog


class TestDataTransferAddressParsing:
    def test_from_address_data_transfer_prefix(self):
        r = DataTransferNode.from_address("data_transfer.egress")
        assert r is not None and r.node_type == "external"

    def test_from_address_aws_dotted_prefix(self):
        r = DataTransferNode.from_address("aws.datatransfer.egress")
        assert r is not None and r.node_type == "external"

    def test_from_address_aws_colon_prefix(self):
        r = DataTransferNode.from_address("aws:datatransfer:inter-region")
        assert r is not None and r.node_type == "external"

    def test_from_address_unrelated(self):
        assert DataTransferNode.from_address("aws_lambda_function.handler") is None
        assert DataTransferNode.from_address("stripe.payment") is None


class TestDataTransferExtraction:
    def test_extract_tf(self):
        resource = {
            "address": "data_transfer.egress",
            "values": {
                "inter_region_gb": 100,
                "internet_out_gb": 200,
                "inter_az_gb": 50,
                "region": "us-east-1",
            },
        }
        result = DataTransferNode.extract_tf(resource)
        assert result.node_type == "external"
        assert result.provider == "aws"
        assert result.service == "AWSDataTransfer"
        assert result.config["interRegionGb"] == 100
        assert result.config["internetOutGb"] == 200
        assert result.config["interAzGb"] == 50

    def test_extract_tf_defaults(self):
        resource = {"address": "data_transfer.egress", "values": {}}
        result = DataTransferNode.extract_tf(resource)
        assert result.config["interRegionGb"] == 0
        assert result.config["internetOutGb"] == 0
        assert result.config["interAzGb"] == 0

    def test_extract_pulumi(self):
        resource = {
            "id": "aws.datatransfer.egress",
            "inputs": {
                "interRegionGb": 10,
                "internetOutGb": 20,
                "interAzGb": 5,
                "region": "us-west-2",
            },
        }
        result = DataTransferNode.extract_pulumi(resource)
        assert result.service == "AWSDataTransfer"
        assert result.config["interRegionGb"] == 10
        assert result.config["internetOutGb"] == 20
        assert result.config["interAzGb"] == 5

    def test_extract_cdk(self):
        resource = {
            "Type": "aws:datatransfer",
            "LogicalId": "Egress",
            "Properties": {
                "InterRegionGb": 1,
                "InternetOutGb": 2,
                "InterAzGb": 3,
            },
        }
        result = DataTransferNode.extract_cdk(resource)
        assert result.service == "AWSDataTransfer"
        assert result.config["interRegionGb"] == 1
        assert result.config["internetOutGb"] == 2
        assert result.config["interAzGb"] == 3


class TestDataTransferPricing:
    def setup_method(self):
        self.catalog = PricingCatalog()

    def test_inter_region_gb(self):
        cost = _data_transfer_cost(inter_region_gb=100, catalog=self.catalog,
                                   region="us-east-1")
        assert cost == pytest.approx(2.0, rel=0.01)  # 100 * $0.02

    def test_internet_out_gb(self):
        cost = _data_transfer_cost(internet_out_gb=100, catalog=self.catalog,
                                   region="us-east-1")
        assert cost == pytest.approx(9.0, rel=0.01)  # 100 * $0.09

    def test_inter_az_gb(self):
        cost = _data_transfer_cost(inter_az_gb=100, catalog=self.catalog,
                                   region="us-east-1")
        assert cost == pytest.approx(1.0, rel=0.01)  # 100 * $0.01

    def test_combined(self):
        cost = _data_transfer_cost(inter_region_gb=100, internet_out_gb=100,
                                   inter_az_gb=100, catalog=self.catalog,
                                   region="us-east-1")
        # 2.0 + 9.0 + 1.0
        assert cost == pytest.approx(12.0, rel=0.01)

    def test_zero_usage(self):
        assert _data_transfer_cost(catalog=self.catalog, region="us-east-1") == 0.0


class TestDataTransferNodeType:
    def test_is_external_leaf(self):
        from infra_cost_model.resources.registry import is_leaf_node
        r = DataTransferNode.from_address("data_transfer.egress")
        assert r is not None and r.node_type == "external"
        assert is_leaf_node("external") is True

    def test_valid_metrics(self):
        n = DataTransferNode()
        assert n.valid_metrics == ["interRegionGb", "internetOutGb", "interAzGb"]


class TestDataTransferRegistry:
    def test_in_registry_from_address(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        assert ResourceRegistry.from_address("data_transfer.egress") == DataTransferNode

    def test_extract_via_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        resource = {
            "address": "data_transfer.egress",
            "values": {"inter_region_gb": 100, "region": "us-east-1"},
        }
        result = ResourceRegistry.extract("data_transfer.egress", resource, "terraform")
        assert result is not None
        assert result["provider"] == "aws"
        assert result["service"] == "AWSDataTransfer"
        assert result["nodeType"] == "external"
        assert result["config"]["interRegionGb"] == 100
