"""Tests for CloudWatch Logs resource model (Issue #185)."""
import pytest
from infra_cost_model.resources.cloudwatch import (
    CloudWatchLogGroup,
    CloudWatchMetricAlarm,
    _cloudwatch_log_cost,
    _cloudwatch_metric_cost,
)
from infra_cost_model.pricing.catalog import PricingCatalog


class TestCWAddressParsing:
    def test_from_address_terraform(self):
        r = CloudWatchLogGroup.from_address("aws_cloudwatch_log_group.app_logs")
        assert r is not None and r.node_type == "storage"

    def test_from_address_pulumi(self):
        r = CloudWatchLogGroup.from_address("aws.cloudwatch.LogGroup:app-logs")
        assert r is not None and r.node_type == "storage"

    def test_from_address_cdk(self):
        # CDK synthetic address format: "<Type>:<LogicalId>"
        r = CloudWatchLogGroup.from_address("AWS::Logs::LogGroup:AppLogs")
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


# ---------------------------------------------------------------------------
# CloudWatch Metric Alarms (Issue #209)
# ---------------------------------------------------------------------------


class TestCWAlarmAddressParsing:
    def test_from_address_terraform(self):
        r = CloudWatchMetricAlarm.from_address("aws_cloudwatch_metric_alarm.cpu_high")
        assert r is not None and r.node_type == "storage"

    def test_from_address_pulumi_dot(self):
        r = CloudWatchMetricAlarm.from_address("aws.cloudwatch.MetricAlarm:cpu-high")
        assert r is not None and r.node_type == "storage"

    def test_from_address_pulumi_colon(self):
        r = CloudWatchMetricAlarm.from_address("aws:cloudwatch:MetricAlarm:cpu-high")
        assert r is not None and r.node_type == "storage"

    def test_from_address_cdk(self):
        r = CloudWatchMetricAlarm.from_address("AWS::CloudWatch::Alarm:CpuAlarm")
        assert r is not None and r.node_type == "storage"

    def test_from_address_unrelated(self):
        assert CloudWatchMetricAlarm.from_address("aws_lambda_function.handler") is None
        # AWS::CloudWatch::CompositeAlarm is a distinct type and must not match.
        assert CloudWatchMetricAlarm.from_address(
            "AWS::CloudWatch::CompositeAlarm:MyComposite") is None

    def test_from_address_not_log_group(self):
        # Must not swallow the Log Group address handled by CloudWatchLogGroup.
        assert CloudWatchMetricAlarm.from_address("aws_cloudwatch_log_group.app") is None


class TestCWAlarmExtraction:
    def test_extract_tf(self):
        resource = {
            "address": "aws_cloudwatch_metric_alarm.cpu_high",
            "type": "aws_cloudwatch_metric_alarm",
            "values": {
                "alarm_name": "cpu-high",
                "namespace": "AWS/EC2",
                "metric_name": "CPUUtilization",
                "comparison_operator": "GreaterThanThreshold",
                "period": 300,
                "region": "us-east-1",
            },
        }
        result = CloudWatchMetricAlarm.extract_tf(resource)
        assert result.node_type == "storage"
        assert result.provider == "aws"
        assert result.service == "AmazonCloudWatch"
        assert result.config["name"] == "cpu-high"
        assert result.config["namespace"] == "AWS/EC2"
        assert result.config["metricName"] == "CPUUtilization"
        assert result.config["comparisonOperator"] == "GreaterThanThreshold"
        assert result.config["period"] == 300

    def test_extract_tf_defaults(self):
        resource = {
            "address": "aws_cloudwatch_metric_alarm.minimal",
            "type": "aws_cloudwatch_metric_alarm",
            "values": {"alarm_name": "minimal", "region": "us-east-1"},
        }
        result = CloudWatchMetricAlarm.extract_tf(resource)
        assert result.config["period"] == 0
        assert result.config["namespace"] is None

    def test_extract_pulumi(self):
        resource = {
            "id": "aws.cloudwatch.MetricAlarm:cpu-high",
            "type": "aws.cloudwatch.MetricAlarm",
            "inputs": {
                "name": "cpu-high",
                "namespace": "AWS/Lambda",
                "metricName": "Errors",
                "comparisonOperator": "GreaterThanOrEqualToThreshold",
                "period": 60,
                "region": "us-west-2",
            },
        }
        result = CloudWatchMetricAlarm.extract_pulumi(resource)
        assert result.provider == "aws"
        assert result.config["name"] == "cpu-high"
        assert result.config["metricName"] == "Errors"
        assert result.config["period"] == 60

    def test_extract_cdk(self):
        resource = {
            "Type": "AWS::CloudWatch::Alarm",
            "LogicalId": "CpuAlarm",
            "Properties": {
                "AlarmName": "cpu-high",
                "Namespace": "AWS/ECS",
                "MetricName": "CPUUtilization",
                "ComparisonOperator": "GreaterThanThreshold",
                "Period": 120,
            },
        }
        result = CloudWatchMetricAlarm.extract_cdk(resource)
        assert result.config["name"] == "cpu-high"
        assert result.config["namespace"] == "AWS/ECS"
        assert result.config["metricName"] == "CPUUtilization"
        assert result.config["period"] == 120


class TestCWAlarmPricing:
    def setup_method(self):
        self.catalog = PricingCatalog()

    def test_custom_metrics_only(self):
        cost = _cloudwatch_metric_cost(custom_metrics_count=10, catalog=self.catalog)
        assert cost == pytest.approx(3.00, rel=0.01)

    def test_alarms_only(self):
        cost = _cloudwatch_metric_cost(alarms_count=5, catalog=self.catalog)
        assert cost == pytest.approx(0.50, rel=0.01)

    def test_get_metric_data_free_tier(self):
        # Within the 1,000,000 free tier -> $0.
        cost = _cloudwatch_metric_cost(
            get_metric_data_requests=500_000, catalog=self.catalog)
        assert cost == pytest.approx(0.0, abs=1e-9)

    def test_get_metric_data_paid_tier(self):
        # 2,000,000 requests: first 1M free, next 1M at $0.00001 -> $10.00.
        cost = _cloudwatch_metric_cost(
            get_metric_data_requests=2_000_000, catalog=self.catalog)
        assert cost == pytest.approx(10.00, rel=0.01)

    def test_combined(self):
        cost = _cloudwatch_metric_cost(
            custom_metrics_count=10, alarms_count=5,
            get_metric_data_requests=2_000_000, catalog=self.catalog)
        assert cost == pytest.approx(13.50, rel=0.01)

    def test_zero_usage(self):
        assert _cloudwatch_metric_cost(catalog=self.catalog) == 0.0


class TestCWAlarmNodeType:
    def test_alarm_is_storage_leaf_node(self):
        result = CloudWatchMetricAlarm.from_address(
            "aws_cloudwatch_metric_alarm.test")
        assert result is not None and result.node_type == "storage"
        from infra_cost_model.resources.registry import is_leaf_node
        assert is_leaf_node(result.node_type) is True

    def test_alarm_valid_metrics(self):
        alarm = CloudWatchMetricAlarm()
        assert all(
            m in alarm.valid_metrics
            for m in ["alarmsCount", "customMetricsCount", "getMetricDataRequests"]
        )


class TestCWAlarmRegistryIntegration:
    def test_in_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        assert ResourceRegistry.from_address(
            "aws_cloudwatch_metric_alarm.main") == CloudWatchMetricAlarm

    def test_extract_via_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        resource = {
            "address": "aws_cloudwatch_metric_alarm.main",
            "type": "aws_cloudwatch_metric_alarm",
            "values": {
                "alarm_name": "main",
                "namespace": "AWS/EC2",
                "metric_name": "CPUUtilization",
                "region": "us-east-1",
            },
        }
        result = ResourceRegistry.extract(
            "aws_cloudwatch_metric_alarm.main", resource, "terraform"
        )
        assert result is not None
        assert result["provider"] == "aws"
        assert result["service"] == "AmazonCloudWatch"
        assert result["nodeType"] == "storage"
