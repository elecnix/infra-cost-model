"""AWS WAFv2 web ACL resource model.

Native handler for AWS WAFv2 web ACLs (``aws_wafv2_web_acl``).
- Recurring cost: $/web-ACL-month + $/rule-month (per rule in the ACL)
- Usage cost: $/request inspected

The optional add-on SKUs (Bot Control, Fraud Control / Account Takeover
Prevention, CAPTCHA, intelligent threat mitigation) are separate products and
out of scope here — the same way the ALB handler defers NLB and the KMS handler
defers asymmetric-key requests. Classic WAF (``aws_waf_web_acl``) is a distinct,
retired product and is intentionally not matched.
"""

from typing import Optional
from infra_cost_model.pricing.catalog import PricingCatalog
from .types import RoutingResource, ResourceExtract


class WAFv2WebACL(RoutingResource):
    """AWS WAFv2 web ACL - routing node with per-ACL + per-rule + per-request pricing."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["webAcls", "rules", "requests"]

    @property
    def catalog_metrics(self) -> dict[str, str]:
        return {
            "webAcls": "WAF-WebACL-Month",
            "rules": "WAF-Rule-Month",
            "requests": "WAF-Request",
        }

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["WAFv2WebACL"]:
        if (resource_address.startswith("aws_wafv2_web_acl.") or
                resource_address.startswith("aws.wafv2.WebAcl:") or
                resource_address.startswith("aws:wafv2:WebAcl:") or
                "WAFv2::WebACL:" in resource_address):
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="routing", provider="aws", service="AWSWAF",
            region=values.get("region"),
            config={
                "name": values.get("name"),
                "scope": values.get("scope", "REGIONAL"),
                "ruleCount": len(values.get("rule") or []),
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="routing", provider="aws", service="AWSWAF",
            region=inputs.get("region"),
            config={
                "name": inputs.get("name"),
                "scope": inputs.get("scope", "REGIONAL"),
                "ruleCount": len(inputs.get("rules") or []),
            },
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        properties = resource.get("Properties", {})
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="routing", provider="aws", service="AWSWAF",
            region=None,
            config={
                "name": properties.get("Name"),
                "scope": properties.get("Scope", "REGIONAL"),
                "ruleCount": len(properties.get("Rules") or []),
            },
        )


def _waf_cost(web_acls=1, rules=0, requests=0, *,
              catalog=None, provider: str = "aws", region: str) -> float:
    if catalog is None:
        catalog = PricingCatalog()
    total = 0.0
    if web_acls > 0:
        r = catalog.query(provider, "AWSWAF", region, "WAF-WebACL-Month", web_acls)
        if r and hasattr(r, "total_cost"):
            total += r.total_cost
    if rules > 0:
        r = catalog.query(provider, "AWSWAF", region, "WAF-Rule-Month", rules)
        if r and hasattr(r, "total_cost"):
            total += r.total_cost
    if requests > 0:
        r = catalog.query(provider, "AWSWAF", region, "WAF-Request", requests)
        if r and hasattr(r, "total_cost"):
            total += r.total_cost
    return total
