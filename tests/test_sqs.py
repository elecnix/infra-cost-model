"""Tests for Amazon SQS Queue resource model (Issue #16)."""
import json
import pytest
from infra_cost_model.resources.sqs import SQSQueue, _sqs_cost
from infra_cost_model.pricing.catalog import PricingCatalog

class TestSQSQueueAddressParsing:
    def test_from_address_terraform(self):
        result = SQSQueue.from_address("aws_sqs_queue.orders")
        assert result is not None and result.node_type == "routing"
    def test_from_address_pulumi(self):
        result = SQSQueue.from_address("aws.sqs.Queue:orders-queue")
        assert result is not None and result.node_type == "routing"
    def test_from_address_cdk(self):
        result = SQSQueue.from_address("OrdersStack/OrdersQueue/SQS::Queue")
        assert result is not None and result.node_type == "routing"
    def test_from_address_aws_sqs_format(self):
        assert SQSQueue.from_address("aws:sqs:Queue:dlq-12345") is not None
    def test_from_address_unrelated(self):
        assert SQSQueue.from_address("aws_lambda_function.handler") is None

class TestSQSQueueExtraction:
    def test_extract_tf_with_dlq(self):
        resource = {"address": "aws_sqs_queue.orders", "type": "aws_sqs_queue", "values": {
            "name": "orders-queue", "fifo_queue": False, "region": "us-east-1",
            "visibility_timeout_seconds": 30, "message_retention_seconds": 345600,
            "delay_seconds": 0,
            "redrive_policy": json.dumps({"deadLetterTargetArn": "arn:aws:sqs:us-east-1:123456789:orders-dlq", "maxReceiveCount": 3})}}
        result = SQSQueue.extract_tf(resource)
        assert result.node_type == "routing" and result.provider == "aws"
        assert result.service == "AmazonSQS" and result.config["name"] == "orders-queue"
        assert result.config["fifoQueue"] is False
        assert result.config["visibilityTimeout"] == 30
        assert result.config["redrivePolicy"] is not None
    def test_extract_tf_fifo(self):
        resource = {"address": "aws_sqs_queue.orders_fifo", "type": "aws_sqs_queue", "values": {"name": "orders-queue.fifo", "fifo_queue": True, "region": "eu-west-1"}}
        result = SQSQueue.extract_tf(resource)
        assert result.config["fifoQueue"] is True
    def test_extract_pulumi(self):
        resource = {"id": "aws.sqs.Queue:task-queue", "type": "aws.sqs.Queue", "inputs": {"name": "task-queue", "fifoQueue": False, "region": "us-west-2", "visibilityTimeoutSeconds": 60, "messageRetentionSeconds": 1209600}}
        result = SQSQueue.extract_pulumi(resource)
        assert result.provider == "aws" and result.config["name"] == "task-queue"
    def test_extract_cdk_with_dlq(self):
        resource = {"Type": "AWS::SQS::Queue", "LogicalId": "DeadLetterQueue", "Properties": {"QueueName": "my-dlq", "FifoQueue": False, "VisibilityTimeout": 30, "MessageRetentionPeriod": 1209600, "RedrivePolicy": {"deadLetterTargetArn": "arn:...", "maxReceiveCount": 3}}}
        result = SQSQueue.extract_cdk(resource)
        assert result.config["name"] == "my-dlq" and result.config["fifoQueue"] is False

class TestSQSPricing:
    def setup_method(self): self.catalog = PricingCatalog()
    def test_standard_pricing(self):
        cost = _sqs_cost(messages_sent=5_000_000, fifo=False, catalog=self.catalog, region="us-east-1")
        assert cost == pytest.approx(1.60, rel=0.01)
    def test_fifo_pricing(self):
        cost = _sqs_cost(messages_sent=5_000_000, fifo=True, catalog=self.catalog, region="us-east-1")
        assert cost == pytest.approx(2.00, rel=0.01)
    def test_standard_vs_fifo_difference(self):
        std = _sqs_cost(messages_sent=5_000_000, fifo=False, catalog=self.catalog, region="us-east-1")
        fifo = _sqs_cost(messages_sent=5_000_000, fifo=True, catalog=self.catalog, region="us-east-1")
        assert fifo > std
    def test_dlq_separate_cost(self):
        main = _sqs_cost(messages_sent=3_000_000, messages_received=3_000_000, fifo=False, catalog=self.catalog, region="us-east-1")
        dlq = _sqs_cost(messages_sent=100_000, fifo=False, catalog=self.catalog, region="us-east-1")
        assert main == pytest.approx(2.00, rel=0.01)
        assert dlq == 0.0
    def test_fifo_with_send_and_receive(self):
        cost = _sqs_cost(messages_sent=2_000_000, messages_received=2_000_000, fifo=True, catalog=self.catalog, region="us-east-1")
        assert cost == pytest.approx(1.50, rel=0.01)
    def test_within_free_tier(self):
        assert _sqs_cost(messages_sent=500_000, messages_received=400_000, fifo=False, catalog=self.catalog, region="us-east-1") == 0.0
    def test_within_free_tier_send_only(self):
        assert _sqs_cost(messages_sent=1_000_000, fifo=True, catalog=self.catalog, region="us-east-1") == 0.0
    def test_zero_usage(self):
        assert _sqs_cost(catalog=self.catalog, region="us-east-1") == 0.0

class TestSQSRoutingNode:
    def test_sqs_is_routing_node(self):
        assert SQSQueue.from_address("aws_sqs_queue.test").node_type == "routing"
    def test_sqs_valid_metrics(self):
        q = SQSQueue()
        assert "messagesSent" in q.valid_metrics and "messagesReceived" in q.valid_metrics

class TestSQSRegistryIntegration:
    def test_sqs_in_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        assert ResourceRegistry.from_address("aws_sqs_queue.my_queue") == SQSQueue
    def test_sqs_extract_via_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        resource = {"address": "aws_sqs_queue.my_queue", "type": "aws_sqs_queue", "values": {"name": "my-queue", "fifo_queue": False, "region": "us-east-1"}}
        result = ResourceRegistry.extract("aws_sqs_queue.my_queue", resource, "terraform")
        assert result is not None and result["provider"] == "aws" and result["service"] == "AmazonSQS" and result["nodeType"] == "routing"
