"""Amazon EventBridge Rule resource model.

EventBridge enables event-driven and schedule-triggered patterns.
Pricing: Custom events $1.00/1M (free tier 1M/month),
Schedule invocations $1.00/1M, Archive replay $0.20/1M.
"""

from typing import Optional
from infra_cost_model.pricing.catalog import PricingCatalog
from .types import RoutingResource, ResourceExtract


class EventBridgeRule(RoutingResource):
    """Amazon EventBridge Rule - routing node with event/schedule modes."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["eventsPublished", "eventsMatched"]

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["EventBridgeRule"]:
        if (resource_address.startswith("aws_cloudwatch_event_rule.") or
                resource_address.startswith("aws_eventbridge_rule.") or
                resource_address.startswith("aws.cloudwatch.EventRule:") or
                resource_address.startswith("aws.eventbridge.Rule:") or
                resource_address.startswith("aws:events:Rule:") or
                "Events::Rule" in resource_address):
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


def _eventbridge_cost(events_published=0, events_matched=0, schedule_invocations=0,
                      archive_replay_events=0, catalog=None, region="us-east-1") -> float:
    if catalog is None:
        catalog = PricingCatalog()
    total = 0.0
    billable_custom = max(0, events_published - 1_000_000)
    if billable_custom > 0:
        r = catalog.query("aws", "AmazonEventBridge", region, "EventBridge-CustomEvent", billable_custom)
        if r and hasattr(r, "total_cost"): total += r.total_cost
    if schedule_invocations > 0:
        r = catalog.query("aws", "AmazonEventBridge", region, "EventBridge-Schedule", schedule_invocations)
        if r and hasattr(r, "total_cost"): total += r.total_cost
    if archive_replay_events > 0:
        r = catalog.query("aws", "AmazonEventBridge", region, "EventBridge-ArchiveReplay", archive_replay_events)
        if r and hasattr(r, "total_cost"): total += r.total_cost
    matched_billable = max(0, events_matched - 1_000_000)
    if matched_billable > 0:
        r = catalog.query("aws", "AmazonEventBridge", region, "EventBridge-CustomEvent", matched_billable)
        if r and hasattr(r, "total_cost"): total += r.total_cost
    return total
