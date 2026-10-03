"""Amazon EventBridge Rule resource model.

EventBridge enables event-driven and schedule-triggered patterns.
Pricing: custom events $1.00/1M, archive replay $1.00/1M.
A scheduled rule on the default event bus costs nothing to run (#326).
"""

from typing import Optional
from .types import RoutingResource, ResourceExtract


class EventBridgeRule(RoutingResource):
    """Amazon EventBridge Rule - routing node with event/schedule modes."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["eventsPublished", "eventsMatched", "scheduledInvocations"]

    @property
    def catalog_metrics(self) -> dict[str, str]:
        # A pattern-matched event and a published custom event bill the same
        # row; a scheduled rule on the default bus bills its own.
        return {"eventsPublished": "EventBridge-CustomEvent",
                "eventsMatched": "EventBridge-CustomEvent",
                "scheduledInvocations": "EventBridge-Schedule"}

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["EventBridgeRule"]:
        if (resource_address.startswith("aws_cloudwatch_event_rule.") or
                resource_address.startswith("aws_eventbridge_rule.") or
                resource_address.startswith("aws.cloudwatch.EventRule:") or
                resource_address.startswith("aws.eventbridge.Rule:") or
                resource_address.startswith("aws:events:Rule:") or
                "Events::Rule:" in resource_address):
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="routing", provider="aws", service="AmazonEventBridge",
            region=values.get("region"),
            config={
                "name": values.get("name"),
                "eventPattern": values.get("event_pattern"),
                "scheduleExpression": values.get("schedule_expression"),
                "isEnabled": values.get("is_enabled", True),
                "eventBusName": values.get("event_bus_name", "default"),
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="routing", provider="aws", service="AmazonEventBridge",
            region=inputs.get("region"),
            config={
                "name": inputs.get("name"),
                "eventPattern": inputs.get("eventPattern"),
                "scheduleExpression": inputs.get("scheduleExpression"),
                "isEnabled": inputs.get("isEnabled", True),
                "eventBusName": inputs.get("eventBusName", "default"),
            },
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        properties = resource.get("Properties", {})
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="routing", provider="aws", service="AmazonEventBridge",
            region=None,
            config={
                "name": properties.get("Name"),
                "eventPattern": properties.get("EventPattern"),
                "scheduleExpression": properties.get("ScheduleExpression"),
                "isEnabled": properties.get("State") != "DISABLED",
                "eventBusName": properties.get("EventBusName", "default"),
            },
        )
