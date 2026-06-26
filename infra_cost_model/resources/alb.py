"""Amazon Application Load Balancer resource model.

ALB is the routing node that fronts most AWS web services.
Pricing: ALB-hour (always-on) + LCU consumption across four dimensions.
LCU dimensions: processed bytes (primary), new connections, active connections,
rule evaluations. AWS bills the max LCU across all dimensions per hour.

Network Load Balancer (type=network) is deferred to a follow-up.
"""

from typing import Optional
from infra_cost_model.pricing.catalog import PricingCatalog
from .types import RoutingResource, ResourceExtract


class ApplicationLoadBalancer(RoutingResource):
    """Amazon Application Load Balancer - routing node with always-on + LCU pricing."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["albHours", "processedGb", "newConnections", "activeConnections", "ruleEvaluations"]

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["ApplicationLoadBalancer"]:
        if (resource_address.startswith("aws_lb.") or
                resource_address.startswith("aws_alb.") or
                resource_address.startswith("aws.lb.LoadBalancer:") or
                resource_address.startswith("aws:lb:LoadBalancer:") or
                "ElasticLoadBalancingV2::LoadBalancer" in resource_address):
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="routing", provider="aws", service="AmazonALB",
            region=values.get("region"),
            config={
                "name": values.get("name"),
                "lbType": values.get("load_balancer_type", "application"),
                "internal": values.get("internal", False),
                "idleTimeout": values.get("idle_timeout", 60),
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="routing", provider="aws", service="AmazonALB",
            region=inputs.get("region"),
            config={
                "name": inputs.get("name"),
                "lbType": inputs.get("loadBalancerType", "application"),
                "internal": inputs.get("internal", False),
                "idleTimeout": inputs.get("idleTimeout", 60),
            },
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        properties = resource.get("Properties", {})
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="routing", provider="aws", service="AmazonALB",
            region=None,
            config={
                "name": properties.get("Name"),
                "lbType": properties.get("Type", "application"),
                "internal": properties.get("Scheme") == "internal",
                "idleTimeout": 60,
            },
        )


def _alb_cost(alb_hours=730, processed_gb=0, new_connections=0,
              active_connections=0, rule_evaluations=0, *,
              catalog=None, provider: str = "aws", region: str = "us-east-1") -> float:
    if catalog is None:
        catalog = PricingCatalog()
    total = 0.0
    if alb_hours > 0:
        r = catalog.query(provider, "AmazonALB", region, "ALB-Hour", alb_hours)
        if r and hasattr(r, "total_cost"):
            total += r.total_cost
    if processed_gb > 0:
        r = catalog.query(provider, "AmazonALB", region, "ALB-LCU-ProcessedBytes", processed_gb)
        if r and hasattr(r, "total_cost"):
            total += r.total_cost
    if new_connections > 0:
        r = catalog.query(provider, "AmazonALB", region, "ALB-LCU-NewConnections", new_connections)
        if r and hasattr(r, "total_cost"):
            total += r.total_cost
    if active_connections > 0:
        r = catalog.query(provider, "AmazonALB", region, "ALB-LCU-ActiveConnections", active_connections)
        if r and hasattr(r, "total_cost"):
            total += r.total_cost
    if rule_evaluations > 0:
        r = catalog.query(provider, "AmazonALB", region, "ALB-LCU-RuleEvaluations", rule_evaluations)
        if r and hasattr(r, "total_cost"):
            total += r.total_cost
    return total
