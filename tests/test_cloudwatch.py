"""Tests for CloudWatch Logs resource model (Issue #185)."""
import pytest
from infra_cost_model.resources.cloudwatch import CloudWatchLogGroup, _cloudwatch_log_cost
from infra_cost_model.pricing.catalog import PricingCatalog


class TestCWAddressParsing:
    def test_from_address_terraform(self):
        r = CloudWatchLogGroup.from_address("aws_cloudwatch_log_group.app_logs")
        assert r is not None and r.node_type == "storage"

    def test_from_address_pulumi(self):
        r = CloudWatchLogGroup.from_address("aws.cloudwatch.LogGroup:app-logs")
        assert r is not None and r.node_type == "storage"

    def test_from_address_cdk(self):
        r = CloudWatchLogGroup.from_address("AppStack/AppLogs/Logs::LogGroup")
        assert r is not None and r.node_type == "storage"

    def test_from_address_aws_format(self):
        assert CloudWatchLogGroup.from_address("aws:cloudwatch:LogGroup:app-logs") is not None

    def test_from_address_unrelated(self):
        assert CloudWatchLogGroup.from_address("aws_lambda_function.handler") is None


class TestCWExtraction:
    def test_extract_tf(self):
        resource = {
            "address": "aws_cloudwatch_log_group.app_logs",
            "type": "aws_cloudwatch_log_group",
            "values": {
                "name": "/aws/lambda/app",
                "retention_in_days": 30,
                "region": "us-east-1",
            },
        }
        result = CloudWatchLogGroup.extract_tf(resource)
        assert result.node_type == "storage"
        assert result.provider == "aws"
        assert result.service == "AmazonCloudWatch"
        assert result.config["name"] == "/aws/lambda/app"
        assert result.config["retentionInDays"] == 30

    def test_extract_tf_no_retention(self):
        resource = {
            "address": "aws_cloudwatch_log_group.default",
            "type": "aws_cloudwatch_log_group",
            "values": {"name": "/aws/lambda/default", "region": "us-east-1"},
        }
        result = CloudWatchLogGroup.extract_tf(resource)
        assert result.config["retentionInDays"] == 0

    def test_extract_pulumi(self):
        resource = {
            "id": "aws.cloudwatch.LogGroup:app-logs",
            "type": "aws.cloudwatch.LogGroup",
            "inputs": {
                "name": "/aws/lambda/app-v2",
                "retentionInDays": 60,
                "region": "us-west-2",
            },
        }
        result = CloudWatchLogGroup.extract_pulumi(resource)
        assert result.provider == "aws"
        assert result.config["name"] == "/aws/lambda/app-v2"
        assert result.config["retentionInDays"] == 60

    def test_extract_cdk(self):
        resource = {
            "Type": "AWS::Logs::LogGroup",
            "LogicalId": "AppLogGroup",
            "Properties": {
                "LogGroupName": "/aws/lambda/myapp",
                "RetentionInDays": 14,
            },
        }
        result = CloudWatchLogGroup.extract_cdk(resource)
        assert result.config["name"] == "/aws/lambda/myapp"
        assert result.config["retentionInDays"] == 14


class TestCWPricing:
    def setup_method(self):
        self.catalog = PricingCatalog()

    def test_ingestion_only(self):
        cost = _cloudwatch_log_cost(ingested_gb=10, catalog=self.catalog)
        assert cost == pytest.approx(5.00, rel=0.01)

    def test_storage_only(self):
        cost = _cloudwatch_log_cost(stored_gb=50, catalog=self.catalog)
        assert cost == pytest.approx(1.50, rel=0.01)

    def test_combined(self):
        cost = _cloudwatch_log_cost(ingested_gb=10, stored_gb=50,
                                    catalog=self.catalog)
        assert cost == pytest.approx(6.50, rel=0.01)

    def test_zero_usage(self):
        assert _cloudwatch_log_cost(catalog=self.catalog) == 0.0


class TestCWNodeType:
    def test_cloudwatch_is_storage_leaf_node(self):
        result = CloudWatchLogGroup.from_address("aws_cloudwatch_log_group.test")
        assert result is not None and result.node_type == "storage"
        from infra_cost_model.resources.registry import is_leaf_node
        assert is_leaf_node("storage") is True
        assert is_leaf_node(result.node_type) is True

    def test_cloudwatch_valid_metrics(self):
        lg = CloudWatchLogGroup()
        assert all(m in lg.valid_metrics for m in ["ingestedGb", "storedGb"])


class TestCWRegistryIntegration:
    def test_in_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        assert ResourceRegistry.from_address("aws_cloudwatch_log_group.main") == CloudWatchLogGroup

    def test_extract_via_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        resource = {
            "address": "aws_cloudwatch_log_group.main",
            "type": "aws_cloudwatch_log_group",
            "values": {
                "name": "/aws/lambda/main",
                "retention_in_days": 7,
                "region": "us-east-1",
            },
        }
        result = ResourceRegistry.extract(
            "aws_cloudwatch_log_group.main", resource, "terraform"
        )
        assert result is not None
        assert result["provider"] == "aws"
        assert result["service"] == "AmazonCloudWatch"
        assert result["nodeType"] == "storage"
