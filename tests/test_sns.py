"""Tests for Amazon SNS Topic resource model (Issue #15)."""
import pytest
from infra_cost_model.resources.sns import SNSTopic, _sns_cost
from infra_cost_model.pricing.catalog import PricingCatalog

class TestSNSTopicAddressParsing:
    def test_from_address_terraform(self):
        r = SNSTopic.from_address("aws_sns_topic.events")
        assert r is not None and r.node_type == "routing"
    def test_from_address_pulumi(self):
        r = SNSTopic.from_address("aws.sns.Topic:my-topic")
        assert r is not None and r.node_type == "routing"
    def test_from_address_cdk(self):
        r = SNSTopic.from_address("EventsStack/MyTopic/SNS::Topic")
        assert r is not None and r.node_type == "routing"
    def test_from_address_aws_sns_format(self):
        assert SNSTopic.from_address("aws:sns:Topic:notifications") is not None
    def test_from_address_unrelated(self):
        assert SNSTopic.from_address("aws_sqs_queue.orders") is None

class TestSNSTopicExtraction:
    def test_extract_tf(self):
        resource = {"address": "aws_sns_topic.events", "type": "aws_sns_topic", "values": {"name": "events-topic", "fifo_topic": False, "region": "us-east-1"}}
        result = SNSTopic.extract_tf(resource)
        assert result.node_type == "routing" and result.provider == "aws" and result.service == "AmazonSNS"
        assert result.config["name"] == "events-topic" and result.config["fifoTopic"] is False
    def test_extract_tf_fifo(self):
        resource = {"address": "aws_sns_topic.important_events", "type": "aws_sns_topic", "values": {"name": "important-events.fifo", "fifo_topic": True, "region": "us-east-1"}}
        assert SNSTopic.extract_tf(resource).config["fifoTopic"] is True
    def test_extract_pulumi(self):
        resource = {"id": "aws.sns.Topic:notifications", "type": "aws.sns.Topic", "inputs": {"name": "my-notifications", "fifoTopic": False, "region": "us-west-2"}}
        result = SNSTopic.extract_pulumi(resource)
        assert result.provider == "aws" and result.config["name"] == "my-notifications"
    def test_extract_cdk_with_subscriptions(self):
        resource = {"Type": "AWS::SNS::Topic", "LogicalId": "EventBus", "Properties": {"TopicName": "my-event-bus", "FifoTopic": False, "Subscription": [{"Endpoint": "arn:aws:sqs:...:queue1", "Protocol": "sqs"}]}}
        result = SNSTopic.extract_cdk(resource)
        assert result.config["name"] == "my-event-bus"

class TestSNSPricing:
    def setup_method(self): self.catalog = PricingCatalog()
    def test_fan_out_basic(self):
        cost = _sns_cost(publishes=2_000_000, sqs_deliveries=2_000_000, lambda_deliveries=2_000_000, http_deliveries=2_000_000, catalog=self.catalog)
        expected = 0.50 + 0.50 + 0.60 + 1.235
        assert cost == pytest.approx(expected, rel=0.01)
    def test_delivery_type_differentiation(self):
        sqs_only = _sns_cost(sqs_deliveries=2_000_000, catalog=self.catalog)
        lambda_only = _sns_cost(lambda_deliveries=2_000_000, catalog=self.catalog)
        http_only = _sns_cost(http_deliveries=2_000_000, catalog=self.catalog)
        assert http_only > lambda_only > sqs_only
    def test_within_free_tier(self):
        assert _sns_cost(publishes=500_000, sqs_deliveries=500_000, catalog=self.catalog) == 0.0
    def test_filtered_deliveries(self):
        cost = _sns_cost(publishes=2_000_000, sqs_deliveries=2_000_000, lambda_deliveries=500_000, catalog=self.catalog)
        assert cost == pytest.approx(1.00, rel=0.01)
    def test_zero_usage(self):
        assert _sns_cost(catalog=self.catalog) == 0.0
    def test_http_free_tier_differs(self):
        cost = _sns_cost(http_deliveries=500_000, catalog=self.catalog)
        assert cost == pytest.approx(0.26, rel=0.02)

class TestSNSRoutingNode:
    def test_sns_is_routing_node(self):
        assert SNSTopic.from_address("aws_sns_topic.test").node_type == "routing"
    def test_sns_valid_metrics(self):
        t = SNSTopic()
        assert all(m in t.valid_metrics for m in ["publishes", "sqsDeliveries", "lambdaDeliveries", "httpDeliveries"])

class TestSNSRegistryIntegration:
    def test_sns_in_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        assert ResourceRegistry.from_address("aws_sns_topic.my_topic") == SNSTopic
    def test_sns_extract_via_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        resource = {"address": "aws_sns_topic.my_topic", "type": "aws_sns_topic", "values": {"name": "my-topic", "fifo_topic": False, "region": "us-east-1"}}
        result = ResourceRegistry.extract("aws_sns_topic.my_topic", resource, "terraform")
        assert result is not None and result["provider"] == "aws" and result["service"] == "AmazonSNS" and result["nodeType"] == "routing"
