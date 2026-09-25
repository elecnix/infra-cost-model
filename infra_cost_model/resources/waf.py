"""AWS WAFv2 web ACL resource model.

Native handler for AWS WAFv2 web ACLs (``aws_wafv2_web_acl``).
- Recurring cost: $/web-ACL-month + $/rule-month (per rule in the ACL)
- Usage cost: $/request inspected
- Bot Control and Fraud Control managed rule groups (#395): $/rule-group-month
  and tiered $/request inspected, on top of the web ACL.

The extract states which of these rule groups a web ACL uses. Bot Control
bills its requests at the price of its inspection level: Common
(``botControlRequests``) or Targeted (``botControlTargetedRequests``). Fraud
Control has two rule groups, Account Takeover Prevention (ATP) and Account
Creation Fraud Prevention (ACFP). Their requests share one tiered product.
CAPTCHA, challenge and Anti-DDoS charges are out of scope. Classic WAF
(``aws_waf_web_acl``) is a distinct, retired product and is intentionally not
matched.
"""

from typing import Optional
from infra_cost_model.pricing.catalog import PricingCatalog
from .types import RoutingResource, ResourceExtract

# A web ACL with the scope CLOUDFRONT bills from the AWS "Global-" products
# (location "Any"), so it gets the catalog region "global", as a CloudFront
# distribution does (#385). A regional web ACL keeps its region.
_CLOUDFRONT_SCOPE = "CLOUDFRONT"
_GLOBAL_REGION = "global"


def _region_for_scope(scope, region):
    return _GLOBAL_REGION if scope == _CLOUDFRONT_SCOPE else region


_BOT_CONTROL = "AWSManagedRulesBotControlRuleSet"
_FRAUD_CONTROL = ("AWSManagedRulesATPRuleSet", "AWSManagedRulesACFPRuleSet")

# The key names of each source: Terraform, Pulumi and CloudFormation (CDK).
_KEYS = {
    "tf": {"statement": "statement", "managed": "managed_rule_group_statement",
           "name": "name", "vendor": "vendor_name",
           "configs": "managed_rule_group_configs",
           "bot": "aws_managed_rules_bot_control_rule_set",
           "level": "inspection_level"},
    "pulumi": {"statement": "statement", "managed": "managedRuleGroupStatement",
               "name": "name", "vendor": "vendorName",
               "configs": "managedRuleGroupConfigs",
               "bot": "awsManagedRulesBotControlRuleSet",
               "level": "inspectionLevel"},
    "cdk": {"statement": "Statement", "managed": "ManagedRuleGroupStatement",
            "name": "Name", "vendor": "VendorName",
            "configs": "ManagedRuleGroupConfigs",
            "bot": "AWSManagedRulesBotControlRuleSet",
            "level": "InspectionLevel"},
}


def _block(value):
    """Return the one block of *value*: Terraform plans state blocks as lists."""
    if isinstance(value, list):
        return value[0] if value else {}
    return value or {}


def _managed_rule_groups(rules, keys) -> dict:
    """Find the Bot Control and Fraud Control rule groups in *rules*.

    Returns the config keys ``botControlInspectionLevel`` (``COMMON``,
    ``TARGETED``, or ``None`` without Bot Control) and
    ``fraudControlRuleGroups`` (the names of the Fraud Control rule groups).
    """
    level = None
    fraud = []
    for rule in rules or []:
        statement = _block(_block(rule).get(keys["statement"]))
        managed = _block(statement.get(keys["managed"]))
        if managed.get(keys["vendor"]) != "AWS":
            continue
        name = managed.get(keys["name"])
        if name == _BOT_CONTROL:
            level = "COMMON"
            for config in managed.get(keys["configs"]) or []:
                bot = _block(_block(config).get(keys["bot"]))
                if bot.get(keys["level"]):
                    level = bot[keys["level"]]
        elif name in _FRAUD_CONTROL:
            fraud.append(name)
    return {"botControlInspectionLevel": level, "fraudControlRuleGroups": fraud}


class WAFv2WebACL(RoutingResource):
    """AWS WAFv2 web ACL - routing node with per-ACL + per-rule + per-request pricing."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["webAcls", "rules", "requests",
                "botControlRuleGroups", "botControlRequests",
                "botControlTargetedRequests",
                "fraudControlRuleGroups", "fraudControlRequests"]

    @property
    def catalog_metrics(self) -> dict[str, str]:
        return {
            "webAcls": "WAF-WebACL-Month",
            "rules": "WAF-Rule-Month",
            "requests": "WAF-Request",
            "botControlRuleGroups": "WAF-BotControl-Month",
            "botControlRequests": "WAF-BotControl-Request",
            "botControlTargetedRequests": "WAF-BotControl-Targeted-Request",
            "fraudControlRuleGroups": "WAF-FraudControl-Month",
            "fraudControlRequests": "WAF-FraudControl-Request",
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
            region=_region_for_scope(values.get("scope"), values.get("region")),
            config={
                "name": values.get("name"),
                "scope": values.get("scope", "REGIONAL"),
                "ruleCount": len(values.get("rule") or []),
                **_managed_rule_groups(values.get("rule"), _KEYS["tf"]),
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="routing", provider="aws", service="AWSWAF",
            region=_region_for_scope(inputs.get("scope"), inputs.get("region")),
            config={
                "name": inputs.get("name"),
                "scope": inputs.get("scope", "REGIONAL"),
                "ruleCount": len(inputs.get("rules") or []),
                **_managed_rule_groups(inputs.get("rules"), _KEYS["pulumi"]),
            },
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        properties = resource.get("Properties", {})
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="routing", provider="aws", service="AWSWAF",
            region=_region_for_scope(properties.get("Scope"), None),
            config={
                "name": properties.get("Name"),
                "scope": properties.get("Scope", "REGIONAL"),
                "ruleCount": len(properties.get("Rules") or []),
                **_managed_rule_groups(properties.get("Rules"), _KEYS["cdk"]),
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
