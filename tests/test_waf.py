"""Tests for the AWS WAFv2 web ACL resource handler (Issue #234)."""
import pytest
from infra_cost_model.pricing.catalog import PricingCatalog
from infra_cost_model.resources.waf import WAFv2WebACL, _waf_cost


class TestWAFAddress:
    def test_from_address_terraform(self):
        r = WAFv2WebACL.from_address("aws_wafv2_web_acl.admin")
        assert r is not None and r.node_type == "routing"

    def test_from_address_pulumi(self):
        r = WAFv2WebACL.from_address("aws.wafv2.WebAcl:edge")
        assert r is not None and r.node_type == "routing"

    def test_from_address_cdk(self):
        # CDK synthetic address format: "<Type>:<LogicalId>"
        r = WAFv2WebACL.from_address("AWS::WAFv2::WebACL:EdgeAcl")
        assert r is not None and r.node_type == "routing"

    def test_from_address_unrelated(self):
        assert WAFv2WebACL.from_address("aws_lb.public") is None
        assert WAFv2WebACL.from_address("aws_kms_key.main") is None
        # Classic WAF (aws_waf_web_acl) is a distinct, retired product; must not match.
        assert WAFv2WebACL.from_address("aws_waf_web_acl.legacy") is None
        # A rule group is a distinct resource, not the web ACL.
        assert WAFv2WebACL.from_address("aws_wafv2_rule_group.rg") is None


class TestWAFExtract:
    def test_extract_tf(self):
        resource = {
            "address": "aws_wafv2_web_acl.admin",
            "values": {
                "name": "admin-waf",
                "scope": "REGIONAL",
                "region": "us-east-1",
                "default_action": [{"allow": [{}]}],
                "rule": [{"name": "common"}, {"name": "bad-inputs"}],
            },
        }
        result = WAFv2WebACL.extract_tf(resource)
        assert result.node_type == "routing"
        assert result.provider == "aws"
        assert result.service == "AWSWAF"
        assert result.region == "us-east-1"
        assert result.config["name"] == "admin-waf"
        assert result.config["scope"] == "REGIONAL"
        assert result.config["ruleCount"] == 2

    def test_extract_tf_defaults(self):
        resource = {
            "address": "aws_wafv2_web_acl.simple",
            "values": {"region": "us-west-2"},
        }
        result = WAFv2WebACL.extract_tf(resource)
        assert result.config["name"] is None
        assert result.config["scope"] == "REGIONAL"
        assert result.config["ruleCount"] == 0

    def test_extract_pulumi(self):
        resource = {
            "id": "aws.wafv2.WebAcl:edge",
            "inputs": {
                "name": "edge-waf",
                "scope": "CLOUDFRONT",
                "region": "us-east-1",
                "rules": [{"name": "a"}, {"name": "b"}, {"name": "c"}],
            },
        }
        result = WAFv2WebACL.extract_pulumi(resource)
        assert result.service == "AWSWAF"
        assert result.config["name"] == "edge-waf"
        assert result.config["scope"] == "CLOUDFRONT"
        assert result.config["ruleCount"] == 3

    def test_extract_pulumi_defaults(self):
        resource = {"id": "aws.wafv2.WebAcl:plain", "inputs": {}}
        result = WAFv2WebACL.extract_pulumi(resource)
        assert result.config["name"] is None
        assert result.config["scope"] == "REGIONAL"
        assert result.config["ruleCount"] == 0

    def test_extract_cdk(self):
        resource = {
            "Type": "AWS::WAFv2::WebACL",
            "LogicalId": "EdgeAcl",
            "Properties": {
                "Name": "cdk-waf",
                "Scope": "CLOUDFRONT",
                "Rules": [{"Name": "a"}],
            },
        }
        result = WAFv2WebACL.extract_cdk(resource)
        assert result.service == "AWSWAF"
        assert result.config["name"] == "cdk-waf"
        assert result.config["scope"] == "CLOUDFRONT"
        assert result.config["ruleCount"] == 1

    def test_extract_cdk_defaults(self):
        resource = {
            "Type": "AWS::WAFv2::WebACL",
            "LogicalId": "PlainAcl",
            "Properties": {},
        }
        result = WAFv2WebACL.extract_cdk(resource)
        assert result.config["name"] is None
        assert result.config["scope"] == "REGIONAL"
        assert result.config["ruleCount"] == 0


class TestWAFNodeAndMetrics:
    def test_node_type(self):
        assert WAFv2WebACL().node_type == "routing"

    def test_valid_metrics(self):
        waf = WAFv2WebACL()
        assert "webAcls" in waf.valid_metrics
        assert "rules" in waf.valid_metrics
        assert "requests" in waf.valid_metrics

    def test_catalog_metrics(self):
        waf = WAFv2WebACL()
        assert waf.catalog_metrics["webAcls"] == "WAF-WebACL-Month"
        assert waf.catalog_metrics["rules"] == "WAF-Rule-Month"
        assert waf.catalog_metrics["requests"] == "WAF-Request"


class TestWAFPricing:
    def test_pricing_single_web_acl(self):
        catalog = PricingCatalog(seed=True)
        cost = _waf_cost(web_acls=1, rules=0, requests=0,
                         catalog=catalog, region="us-east-1")
        assert cost == pytest.approx(5.00, rel=0.01)

    def test_pricing_rules(self):
        catalog = PricingCatalog(seed=True)
        # 1 web ACL ($5) + 4 rules ($4) = $9
        cost = _waf_cost(web_acls=1, rules=4, requests=0,
                         catalog=catalog, region="us-east-1")
        assert cost == pytest.approx(9.00, rel=0.01)

    def test_pricing_requests(self):
        catalog = PricingCatalog(seed=True)
        # 1M requests at $0.60/million
        cost = _waf_cost(web_acls=0, rules=0, requests=1_000_000,
                         catalog=catalog, region="us-east-1")
        assert cost == pytest.approx(0.60, rel=0.01)

    def test_pricing_combined(self):
        catalog = PricingCatalog(seed=True)
        # 1 web ACL ($5) + 4 rules ($4) + 1M requests ($0.60) = $9.60
        cost = _waf_cost(web_acls=1, rules=4, requests=1_000_000,
                         catalog=catalog, region="us-east-1")
        assert cost == pytest.approx(9.60, rel=0.01)

    def test_pricing_zero_usage(self):
        catalog = PricingCatalog(seed=True)
        cost = _waf_cost(web_acls=0, rules=0, requests=0,
                         catalog=catalog, region="us-east-1")
        assert cost == 0.0


class TestWAFRegistry:
    def test_registry_from_address(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        assert ResourceRegistry.from_address("aws_wafv2_web_acl.admin") == WAFv2WebACL

    def test_extract_via_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        resource = {
            "address": "aws_wafv2_web_acl.admin",
            "values": {"name": "admin-waf", "scope": "REGIONAL", "region": "us-east-1"},
        }
        result = ResourceRegistry.extract(
            "aws_wafv2_web_acl.admin", resource, "terraform"
        )
        assert result is not None
        assert result["provider"] == "aws"
        assert result["service"] == "AWSWAF"
        assert result["nodeType"] == "routing"
