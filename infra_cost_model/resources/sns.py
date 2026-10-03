"""Amazon SNS Topic resource model.

SNS is the pub/sub hub for event-driven architectures.
Pricing covers publishes (/bin/bash.50/1M) and per-endpoint deliveries:
SQS (/bin/bash.50/1M), Lambda (/bin/bash.60/1M), HTTP (/bin/bash.65/1M).
"""

from typing import Optional
from .types import RoutingResource, ResourceExtract


class SNSTopic(RoutingResource):
    """Amazon SNS Topic - routing node with fan-out pricing."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["publishes", "sqsDeliveries", "lambdaDeliveries", "httpDeliveries"]

    @property
    def catalog_metrics(self) -> dict[str, str]:
        return {"publishes": "SNS-Publish",
                "sqsDeliveries": "SNS-Delivery-SQS",
                "lambdaDeliveries": "SNS-Delivery-Lambda",
                "httpDeliveries": "SNS-Delivery-HTTP"}

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["SNSTopic"]:
        if (resource_address.startswith("aws_sns_topic.") or
                resource_address.startswith("aws.sns.Topic:") or
                resource_address.startswith("aws:sns:Topic:") or
                "SNS::Topic:" in resource_address):
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="routing", provider="aws", service="AmazonSNS",
            region=values.get("region"),
            config={"name": values.get("name"), "fifoTopic": values.get("fifo_topic", False)},
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="routing", provider="aws", service="AmazonSNS",
            region=inputs.get("region"),
            config={"name": inputs.get("name"), "fifoTopic": inputs.get("fifoTopic", False)},
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        properties = resource.get("Properties", {})
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="routing", provider="aws", service="AmazonSNS",
            region=None,
            config={"name": properties.get("TopicName"), "fifoTopic": properties.get("FifoTopic", False)},
        )
