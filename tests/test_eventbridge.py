"""Tests for Amazon EventBridge Rule resource model (Issue #18)."""
import pytest
from infra_cost_model.resources.eventbridge import EventBridgeRule, _eventbridge_cost
from infra_cost_model.pricing.catalog import PricingCatalog

class TestEventBridgeAddressParsing:
    def test_from_address_terraform_cloudwatch(self):
        r = EventBridgeRule.from_address("aws_cloudwatch_event_rule.daily")
        assert r is not None and r.node_type == "routing"
    def test_from_address_terraform_eventbridge(self):
        r = EventBridgeRule.from_address("aws_eventbridge_rule.pattern")
        assert r is not None and r.node_type == "routing"
    def test_from_address_pulumi(self):
        assert EventBridgeRule.from_address("aws.cloudwatch.EventRule:cron") is not None
    def test_from_address_cdk(self):
        assert EventBridgeRule.from_address("PipelineStack/DailyTrigger/Events::Rule") is not None
    def test_from_address_unrelated(self):
        assert EventBridgeRule.from_address("aws_lambda_function.handler") is None

class TestEventBridgeExtraction:
    def test_extract_tf_event_pattern(self):
        resource = {"address": "aws_cloudwatch_event_rule.order_events", "type": "aws_cloudwatch_event_rule", "values": {"name": "order-events", "region": "us-east-1", "event_pattern": '{"source":["myapp.orders"]}', "schedule_expression": None, "is_enabled": True, "event_bus_name": "default"}}
        result = EventBridgeRule.extract_tf(resource)
        assert result.node_type == "routing" and result.provider == "aws" and result.service == "AmazonEventBridge"
        assert result.config["eventPattern"] is not None and result.config["scheduleExpression"] is None
    def test_extract_tf_schedule(self):
        resource = {"address": "aws_cloudwatch_event_rule.daily_job", "type": "aws_cloudwatch_event_rule", "values": {"name": "daily-job", "region": "us-east-1", "event_pattern": None, "schedule_expression": "rate(1 day)", "is_enabled": True}}
        result = EventBridgeRule.extract_tf(resource)
        assert result.config["scheduleExpression"] == "rate(1 day)" and result.config["eventPattern"] is None
    def test_extract_pulumi(self):
        resource = {"id": "aws.cloudwatch.EventRule:cron", "type": "aws.cloudwatch.EventRule", "inputs": {"name": "my-rule", "scheduleExpression": "cron(0 12 * * ? *)", "region": "us-east-1"}}
        assert EventBridgeRule.extract_pulumi(resource).provider == "aws"
    def test_extract_cdk(self):
        resource = {"Type": "AWS::Events::Rule", "LogicalId": "OrderProcessor", "Properties": {"Name": "order-processor", "EventPattern": {"source": ["myapp.orders"]}, "State": "ENABLED", "EventBusName": "default"}}
        result = EventBridgeRule.extract_cdk(resource)
        assert result.config["name"] == "order-processor" and result.config["eventPattern"] is not None and result.config["isEnabled"] is True

class TestEventBridgePricing:
    def setup_method(self): self.catalog = PricingCatalog()
    def test_custom_event_pricing(self):
        cost = _eventbridge_cost(events_published=3_000_000, catalog=self.catalog, region="us-east-1")
        assert cost == pytest.approx(2.00, rel=0.01)
    def test_schedule_pricing_no_free_tier(self):
        cost = _eventbridge_cost(schedule_invocations=1_000_000, catalog=self.catalog, region="us-east-1")
        assert cost == pytest.approx(1.00, rel=0.01)
    def test_event_vs_schedule_difference(self):
        assert _eventbridge_cost(events_published=500_000, catalog=self.catalog, region="us-east-1") == 0.0
        assert _eventbridge_cost(schedule_invocations=500_000, catalog=self.catalog, region="us-east-1") == pytest.approx(0.50, rel=0.01)
    def test_fan_out_multiple_rules(self):
        cost = _eventbridge_cost(events_published=3_000_000, events_matched=6_000_000, catalog=self.catalog, region="us-east-1")
        assert cost == pytest.approx(7.00, rel=0.01)
    def test_archive_replay(self):
        cost = _eventbridge_cost(archive_replay_events=5_000_000, catalog=self.catalog, region="us-east-1")
        assert cost == pytest.approx(1.00, rel=0.02)
    def test_content_filtering_reduces_cost(self):
        cost = _eventbridge_cost(events_published=2_000_000, events_matched=200_000, catalog=self.catalog, region="us-east-1")
        assert cost == pytest.approx(1.00, rel=0.01)
    def test_within_free_tier(self):
        assert _eventbridge_cost(events_published=500_000, events_matched=500_000, catalog=self.catalog, region="us-east-1") == 0.0
    def test_zero_usage(self):
        assert _eventbridge_cost(catalog=self.catalog, region="us-east-1") == 0.0

class TestEventBridgeRoutingNode:
    def test_is_routing_node(self):
        assert EventBridgeRule.from_address("aws_cloudwatch_event_rule.test").node_type == "routing"
    def test_valid_metrics(self):
        r = EventBridgeRule()
        assert "eventsPublished" in r.valid_metrics and "eventsMatched" in r.valid_metrics

class TestEventBridgeRegistryIntegration:
    def test_in_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        assert ResourceRegistry.from_address("aws_cloudwatch_event_rule.my_rule") == EventBridgeRule
    def test_extract_via_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        resource = {"address": "aws_cloudwatch_event_rule.my_rule", "type": "aws_cloudwatch_event_rule", "values": {"name": "my-rule", "schedule_expression": "rate(5 minutes)", "region": "us-east-1"}}
        result = ResourceRegistry.extract("aws_cloudwatch_event_rule.my_rule", resource, "terraform")
        assert result is not None and result["provider"] == "aws" and result["service"] == "AmazonEventBridge" and result["nodeType"] == "routing"
