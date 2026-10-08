"""Amazon SQS Queue resource model.

SQS is the queuing backbone for event-driven architectures.
Pricing covers:
- Standard queue requests: $0.40/1M
- FIFO queue requests: $0.50/1M
- Free tier: 1M requests/month, shared by standard and FIFO queues (#338)

AWS doesn't charge for storing messages in a queue (#324).

SQS is a routing node — it can forward messages to Lambda consumers.
Dead-letter queues are modeled as separate SQS nodes with their own cost.
"""

from typing import Optional


from .types import RoutingResource, ResourceExtract


class SQSQueue(RoutingResource):
    """Amazon SQS Queue - routing node with standard/FIFO pricing models."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["messagesSent", "messagesReceived"]

    @property
    def catalog_metrics(self) -> dict[str, str]:
        # Sending and receiving are each one request, and both bill the same row.
        return {"messagesSent": "SQS-Standard-Request",
                "messagesReceived": "SQS-Standard-Request"}

    def catalog_metrics_for(self, config: dict) -> dict[str, str]:
        """A FIFO queue prices its requests under the FIFO row."""
        if not (config or {}).get("fifoQueue"):
            return self.catalog_metrics
        return {"messagesSent": "SQS-FIFO-Request",
                "messagesReceived": "SQS-FIFO-Request"}

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["SQSQueue"]:
        if (resource_address.startswith("aws_sqs_queue.") or
                resource_address.startswith("aws.sqs.Queue:") or
                resource_address.startswith("aws:sqs:Queue:") or
                "SQS::Queue:" in resource_address):
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
