"""Amazon Application Load Balancer resource model.

ALB is the routing node that fronts most AWS web services.
Pricing: ALB-hour (always-on) + LCU consumption across four dimensions.
LCU dimensions: processed bytes (primary), new connections, active connections,
rule evaluations. AWS bills the max LCU across all dimensions per hour, so
``derive_catalog_usage`` prices the largest dimension once (#480).

Network Load Balancer (type=network) is deferred to a follow-up.
"""

from typing import Optional
from .types import DerivedCatalogUsage, RoutingResource, ResourceExtract


class ApplicationLoadBalancer(RoutingResource):
    """Amazon Application Load Balancer - routing node with always-on + LCU pricing."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["albHours", "processedGb", "newConnections", "activeConnections", "ruleEvaluations"]

    @property
    def catalog_metrics(self) -> dict[str, str]:
        return {"albHours": "ALB-Hour",
                "processedGb": "ALB-LCU-ProcessedBytes",
                "newConnections": "ALB-LCU-NewConnections",
                "activeConnections": "ALB-LCU-ActiveConnections",
                "ruleEvaluations": "ALB-LCU-RuleEvaluations"}

    LCU_DIMENSIONS = ("processedGb", "newConnections", "activeConnections",
                      "ruleEvaluations")

    def derive_catalog_usage(self, usage: dict[str, float],
                             config: Optional[dict] = None) -> Optional[DerivedCatalogUsage]:
        """Price the largest LCU dimension once, the way AWS bills it.

        Each LCU metric counts LCU-hours of its own dimension. AWS bills
        an hour for the dimension that used the most LCUs that hour, not for
        all four. Summing the dimensions, as one catalog row per metric did,
        overstated the bill (#480).

        The engine passes quantities per node invocation, and multiplies the
        result by the month's invocations. The largest quantity per
        invocation therefore picks the largest monthly total. That equals the
        hourly maximum AWS bills when the load is spread evenly over the
        month, so each hour sees the same mix of dimensions. A spiky load
        can make a different dimension the largest in different hours. AWS
        can then bill more than this estimate, but never more than the sum.

        The four LCU rows share one price, so the maximum prices through the
        ``ALB-LCU-ProcessedBytes`` row. With one LCU dimension, the handler
        derives nothing and the metric keeps its own row.

        Only usage-driven metrics reach this method. An LCU metric marked
        ``fixed`` goes through the per-metric path, so its cost adds to the
        maximum of the others. Model every LCU dimension as usage-driven. To
        state LCUs as a fixed monthly total, list only the largest dimension.
        """
        present = [name for name in self.LCU_DIMENSIONS if name in usage]
        if len(present) < 2:
            return None
        return DerivedCatalogUsage(
            consumed=frozenset(present),
            quantities={"ALB-LCU-ProcessedBytes":
                        max(usage[name] for name in present)},
        )

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["ApplicationLoadBalancer"]:
        if (resource_address.startswith("aws_lb.") or
                resource_address.startswith("aws_alb.") or
                resource_address.startswith("aws.lb.LoadBalancer:") or
                resource_address.startswith("aws:lb:LoadBalancer:") or
                "ElasticLoadBalancingV2::LoadBalancer:" in resource_address):
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
