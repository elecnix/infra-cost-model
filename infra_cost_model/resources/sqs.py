"""Amazon SQS Queue resource model.

SQS is the queuing backbone for event-driven architectures.
Pricing covers:
- Standard queue requests: $0.40/1M (free tier: 1M/month)
- FIFO queue requests: $0.50/1M (free tier: 1M/month)
- Message retention: $0.023/GB-month

SQS is a routing node — it can forward messages to Lambda consumers.
Dead-letter queues are modeled as separate SQS nodes with their own cost.
"""

from typing import Optional

from infra_cost_model.pricing.catalog import PricingCatalog

from .types import RoutingResource, ResourceExtract


class SQSQueue(RoutingResource):
    """Amazon SQS Queue - routing node with standard/FIFO pricing models."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["messagesSent", "messagesReceived"]

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["SQSQueue"]:
        if (resource_address.startswith("aws_sqs_queue.") or
                resource_address.startswith("aws.sqs.Queue:") or
                resource_address.startswith("aws:sqs:Queue:") or
                "SQS::Queue" in resource_address):
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="routing",
            provider="aws",
            service="AmazonSQS",
            region=values.get("region"),
            config={
                "name": values.get("name"),
                "fifoQueue": values.get("fifo_queue", False),
                "visibilityTimeout": values.get("visibility_timeout_seconds"),
                "messageRetention": values.get("message_retention_seconds"),
                "delaySeconds": values.get("delay_seconds"),
                "redrivePolicy": values.get("redrive_policy"),
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="routing",
            provider="aws",
            service="AmazonSQS",
            region=inputs.get("region"),
            config={
                "name": inputs.get("name"),
                "fifoQueue": inputs.get("fifoQueue", False),
                "visibilityTimeout": inputs.get("visibilityTimeoutSeconds"),
                "messageRetention": inputs.get("messageRetentionSeconds"),
                "delaySeconds": inputs.get("delaySeconds"),
                "redrivePolicy": inputs.get("redrivePolicy"),
            },
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        properties = resource.get("Properties", {})
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="routing",
            provider="aws",
            service="AmazonSQS",
            region=None,
            config={
                "name": properties.get("QueueName"),
                "fifoQueue": properties.get("FifoQueue", False),
                "visibilityTimeout": properties.get("VisibilityTimeout"),
                "messageRetention": properties.get("MessageRetentionPeriod"),
                "delaySeconds": properties.get("DelaySeconds"),
                "redrivePolicy": properties.get("RedrivePolicy"),
            },
        )


def _sqs_cost(
    messages_sent: float = 0,
    messages_received: float = 0,
    fifo: bool = False,
    retention_gb: float = 0,
    catalog=None,
    region: str = "us-east-1",
) -> float:
    """Calculate SQS cost using catalog pricing.

    Standard: $0.40/1M requests (free tier 1M/month).
    FIFO: $0.50/1M requests (free tier 1M/month).
    Retention: $0.023/GB-month.
    """
    if catalog is None:
        catalog = PricingCatalog()

    total = 0.0
    free_tier = 1_000_000
    total_requests = messages_sent + messages_received
    billable_requests = max(0, total_requests - free_tier)

    if billable_requests > 0:
        metric = "SQS-FIFO-Request" if fifo else "SQS-Standard-Request"
        result = catalog.query("aws", "AmazonSQS", region, metric,
                               billable_requests)
        if result and hasattr(result, "total_cost"):
            total += result.total_cost

    if retention_gb > 0:
        result = catalog.query("aws", "AmazonSQS", region, "SQS-Retention",
                               retention_gb)
        if result and hasattr(result, "total_cost"):
            total += result.total_cost

    return total
