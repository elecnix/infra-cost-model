"""Bot Control and Fraud Control managed rule groups on a WAF web ACL (#395).

The AWS price list for ``awswaf`` (published 2026-09-14, checked 2026-09-24)
states these products in us-east-1, and the same prices for the ``Global-``
products that a web ACL with the scope CLOUDFRONT bills from:

- ``USE1-AMR-BotControl``: $10.00 a month for each Bot Control rule group.
- ``USE1-AMR-BotControl-Request``: the first 10 million requests a month
  free, then $1.00 per million (inspection level Common).
- ``USE1-AMR-BotControl-Targeted-Request``: the first 1 million requests a
  month free, then $10.00 per million (inspection level Targeted).
- ``USE1-AMR-ATP``: $10.00 a month for each Fraud Control rule group.
- ``USE1-AMR-FraudControl-Request``: the first 10,000 requests a month free,
  then $1.00, $0.70, $0.40, $0.20 and $0.05 per thousand, with tiers at 2, 5,
  15 and 30 million.

Each region's request product starts with its own $0 tier, so the free
requests apply to each region and to the global scope on their own.

The fake Cloud Pricing API below holds the products that the live API
returned on 2026-09-24. They start with the $0 tier.
"""

import json
import warnings
from unittest.mock import MagicMock, patch

import pytest

from infra_cost_model.engine import CostEngine
from infra_cost_model.pricing.cache import SEED_PRICES_PATH, load_seed_rows
from infra_cost_model.pricing.free_tiers import (
    FREE_ALLOWANCES, REGION, free_tier_scope,
)
from infra_cost_model.pricing.global_services import is_global_metric
from infra_cost_model.pricing.sources import infracost as ic
from infra_cost_model.resources.waf import WAFv2WebACL

INF = "Inf"
WAF = "Web Application Firewall"
NEW_METRICS = {
    "WAF-BotControl-Month": "AMR-BotControl",
    "WAF-BotControl-Request": "AMR-BotControl-Request",
    "WAF-BotControl-Targeted-Request": "AMR-BotControl-Targeted-Request",
    "WAF-FraudControl-Month": "AMR-ATP",
    "WAF-FraudControl-Request": "AMR-FraudControl-Request",
}

BOT_TIERS = [("0", "Request", "0", "10000000"), ("0.000001", "Request", "10000000", INF)]
TARGETED_TIERS = [("0", "Request", "0", "1000000"),
                  ("0.00001", "Request", "1000000", INF)]
FRAUD_TIERS = [("0", "Request", "0", "10000"),
               ("0.001", "Request", "10000", "2000000"),
               ("0.0007", "Request", "2000000", "5000000"),
               ("0.0004", "Request", "5000000", "15000000"),
               ("0.0002", "Request", "15000000", "30000000"),
               ("0.00005", "Request", "30000000", INF)]
MONTH = [("10", "Month", "0", INF)]


def _product(region, usagetype, prices):
    return {
        "region": region, "productFamily": WAF,
        "attributes": [{"key": "usagetype", "value": usagetype}],
        "prices": [{"USD": usd, "unit": unit, "startUsageAmount": start,
                    "endUsageAmount": end} for usd, unit, start, end in prices],
    }


def _both(suffix, prices):
    return [_product("us-east-1", f"USE1-{suffix}", prices),
            _product("", f"Global-{suffix}", prices),
            _product("us-east-1", f"USE1-ShieldProtected-{suffix}", prices)]


CATALOGUE = (_both("AMR-BotControl", MONTH)
             + _both("AMR-BotControl-Request", BOT_TIERS)
             + _both("AMR-BotControl-Targeted-Request", TARGETED_TIERS)
             + _both("AMR-ATP", MONTH)
             + _both("AMR-FraudControl-Request", FRAUD_TIERS))


def _fake_post(captured):
    def post(url, headers=None, json=None, timeout=None):
        variables = json["variables"]
        captured.append(variables)
        filters = variables.get("attributeFilters") or []
        matched = [
            p for p in CATALOGUE
            if p["region"] == variables["region"]
            and p["productFamily"] == variables.get("productFamily")
            and all({a["key"]: a["value"] for a in p["attributes"]}.get(f["key"])
                    == f["value"] for f in filters)
        ]
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"data": {"products": matched}}
        resp.raise_for_status.return_value = None
        return resp
    return post


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("INFRACOST_API_KEY", "test-token")
    monkeypatch.setenv("INFRACOST_ORG_ID", "org-123")


def _sync(metric, region, captured=None):
    captured = [] if captured is None else captured
    upserted = []
    cache = MagicMock()
    cache.upsert.side_effect = upserted.append
    with patch.object(ic.requests, "post", side_effect=_fake_post(captured)):
        ic.InfracostClient().sync_to_cache(cache, metric, region)
    return upserted


def _tiers(rows):
    return sorted((r.price_usd, r.start_usage_amount or 0.0,
                   None if r.end_usage_amount in (None, float("inf"))
                   else r.end_usage_amount) for r in rows)


# --- Detecting the rule groups ----------------------------------------------------

def _tf_rule(name, level=None):
    configs = []
    if level:
        configs = [{"aws_managed_rules_bot_control_rule_set": [
            {"inspection_level": level}]}]
    return {"name": name, "statement": [{"managed_rule_group_statement": [{
        "name": name, "vendor_name": "AWS", "managed_rule_group_configs": configs,
    }]}]}


def test_tf_detects_bot_control_and_fraud_control():
    resource = {"address": "aws_wafv2_web_acl.api", "values": {
        "scope": "REGIONAL", "region": "us-east-1",
        "rule": [
            _tf_rule("AWSManagedRulesBotControlRuleSet", "TARGETED"),
            _tf_rule("AWSManagedRulesATPRuleSet"),
            _tf_rule("AWSManagedRulesACFPRuleSet"),
            _tf_rule("AWSManagedRulesCommonRuleSet"),
            {"name": "rate", "statement": [{"rate_based_statement": [{"limit": 100}]}]},
        ],
    }}
    config = WAFv2WebACL.extract_tf(resource).config
    assert config["ruleCount"] == 5
    assert config["botControlInspectionLevel"] == "TARGETED"
    assert config["fraudControlRuleGroups"] == [
        "AWSManagedRulesATPRuleSet", "AWSManagedRulesACFPRuleSet"]


def test_tf_bot_control_without_a_config_is_common():
    resource = {"address": "aws_wafv2_web_acl.api", "values": {
        "region": "us-east-1", "rule": [_tf_rule("AWSManagedRulesBotControlRuleSet")]}}
    assert WAFv2WebACL.extract_tf(resource).config["botControlInspectionLevel"] == "COMMON"


@pytest.mark.parametrize("levels", [("TARGETED", None), (None, "TARGETED"),
                                    ("TARGETED", "COMMON"), ("COMMON", "TARGETED")])
def test_tf_one_targeted_bot_control_group_sets_the_level(levels):
    resource = {"address": "aws_wafv2_web_acl.api", "values": {
        "region": "us-east-1",
        "rule": [_tf_rule("AWSManagedRulesBotControlRuleSet", level)
                 for level in levels]}}
    assert WAFv2WebACL.extract_tf(resource).config["botControlInspectionLevel"] == "TARGETED"


def test_tf_ignores_a_rule_group_of_another_vendor():
    rule = _tf_rule("AWSManagedRulesBotControlRuleSet")
    rule["statement"][0]["managed_rule_group_statement"][0]["vendor_name"] = "F5"
    resource = {"address": "aws_wafv2_web_acl.api",
                "values": {"region": "us-east-1", "rule": [rule]}}
    config = WAFv2WebACL.extract_tf(resource).config
    assert config["botControlInspectionLevel"] is None
    assert config["fraudControlRuleGroups"] == []


def test_web_acl_without_managed_rule_groups():
    config = WAFv2WebACL.extract_tf({"address": "aws_wafv2_web_acl.a",
                                     "values": {"region": "us-east-1"}}).config
    assert config["botControlInspectionLevel"] is None
    assert config["fraudControlRuleGroups"] == []


def test_pulumi_detects_bot_control_and_fraud_control():
    resource = {"id": "aws.wafv2.WebAcl:edge", "inputs": {
        "scope": "CLOUDFRONT",
        "rules": [
            {"name": "bots", "statement": {"managedRuleGroupStatement": {
                "name": "AWSManagedRulesBotControlRuleSet", "vendorName": "AWS",
                "managedRuleGroupConfigs": [{"awsManagedRulesBotControlRuleSet": {
                    "inspectionLevel": "COMMON"}}]}}},
            {"name": "atp", "statement": {"managedRuleGroupStatement": {
                "name": "AWSManagedRulesATPRuleSet", "vendorName": "AWS"}}},
        ],
    }}
    result = WAFv2WebACL.extract_pulumi(resource)
    assert result.region == "global"
    assert result.config["botControlInspectionLevel"] == "COMMON"
    assert result.config["fraudControlRuleGroups"] == ["AWSManagedRulesATPRuleSet"]


def test_cdk_detects_bot_control_and_fraud_control():
    resource = {"LogicalId": "Acl", "Properties": {
        "Scope": "REGIONAL",
        "Rules": [
            {"Name": "bots", "Statement": {"ManagedRuleGroupStatement": {
                "Name": "AWSManagedRulesBotControlRuleSet", "VendorName": "AWS",
                "ManagedRuleGroupConfigs": [{"AWSManagedRulesBotControlRuleSet": {
                    "InspectionLevel": "TARGETED"}}]}}},
            {"Name": "acfp", "Statement": {"ManagedRuleGroupStatement": {
                "Name": "AWSManagedRulesACFPRuleSet", "VendorName": "AWS"}}},
        ],
    }}
    config = WAFv2WebACL.extract_cdk(resource).config
    assert config["botControlInspectionLevel"] == "TARGETED"
    assert config["fraudControlRuleGroups"] == ["AWSManagedRulesACFPRuleSet"]


def test_handler_maps_the_new_metrics():
    metrics = WAFv2WebACL().catalog_metrics
    assert metrics["botControlRuleGroups"] == "WAF-BotControl-Month"
    assert metrics["botControlRequests"] == "WAF-BotControl-Request"
    assert metrics["botControlTargetedRequests"] == "WAF-BotControl-Targeted-Request"
    assert metrics["fraudControlRuleGroups"] == "WAF-FraudControl-Month"
    assert metrics["fraudControlRequests"] == "WAF-FraudControl-Request"
    assert set(metrics) <= set(WAFv2WebACL().valid_metrics)


# --- Pricing ------------------------------------------------------------------------

def _monthly(catalog, nodes):
    names = list(nodes)
    model = {
        "version": "1.0",
        "workflow": {"name": "w", "entry": names[0],
                     "frequency": {"unit": "perMonth", "value": 1}},
        "nodes": nodes,
        "edges": [{"from": a, "to": b, "type": "async", "rate": 1}
                  for a, b in zip(names, names[1:])],
    }
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return CostEngine(model, catalog=catalog, time_basis="monthly").compute()


def _acl(region, name=None, **usage):
    metrics = {
        key: {"unit": "RuleGroups" if key.endswith("RuleGroups") else "requests",
              "value": value, "fixed": True}
        for key, value in usage.items()
    }
    return {
        "nodeType": "routing",
        "resourceAddress": f"aws_wafv2_web_acl.{name or region}",
        "provider": "aws", "service": "AWSWAF", "region": region,
        "usageMetrics": metrics,
    }


@pytest.mark.parametrize("region", ["us-east-1", "global"])
@pytest.mark.parametrize("millions,want", [(5, 10.0), (20, 10.0 + 10 * 1.0)])
def test_bot_control_common(seed_catalog, region, millions, want):
    costs = _monthly(seed_catalog, {"acl": _acl(
        region, botControlRuleGroups=1, botControlRequests=millions * 1_000_000)})
    assert costs["acl"] == pytest.approx(want)


def test_bot_control_targeted(seed_catalog):
    # 5 million requests: 1 million free, 4 million at $10.00 per million.
    costs = _monthly(seed_catalog, {"acl": _acl(
        "us-east-1", botControlRuleGroups=1, botControlTargetedRequests=5_000_000)})
    assert costs["acl"] == pytest.approx(10 + 40)


def test_each_scope_has_its_own_free_bot_control_requests(seed_catalog):
    # A regional and a CloudFront web ACL with 8 million requests each: both
    # fall inside their own 10 million free requests.
    costs = _monthly(seed_catalog, {
        "api": _acl("us-east-1", "api", botControlRequests=8_000_000),
        "edge": _acl("global", "edge", botControlRequests=8_000_000),
    })
    assert costs["api"] + costs["edge"] == pytest.approx(0)


def test_cloudfront_web_acls_share_the_global_free_requests(seed_catalog):
    # 8 + 8 million requests in the global scope: 6 million at $1.00 per million.
    costs = _monthly(seed_catalog, {
        "a": _acl("global", "a", botControlRequests=8_000_000),
        "b": _acl("global", "b", botControlRequests=8_000_000),
    })
    assert costs["a"] + costs["b"] == pytest.approx(6.0)


@pytest.mark.parametrize("region", ["us-east-1", "global"])
def test_fraud_control_atp(seed_catalog, region):
    # 3 million requests: 10,000 free, 1,990,000 at $1.00 per thousand and
    # 1,000,000 at $0.70 per thousand, plus $10.00 for the rule group.
    costs = _monthly(seed_catalog, {"acl": _acl(
        region, fraudControlRuleGroups=1, fraudControlRequests=3_000_000)})
    assert costs["acl"] == pytest.approx(10 + 1990 + 700)


def test_fraud_control_top_tier(seed_catalog):
    got = seed_catalog.query("aws", "AWSWAF", "us-east-1",
                             "WAF-FraudControl-Request", 40_000_000).total_cost
    want = (1_990_000 * 0.001 + 3_000_000 * 0.0007 + 10_000_000 * 0.0004
            + 15_000_000 * 0.0002 + 10_000_000 * 0.00005)
    assert got == pytest.approx(want)


# --- Seed rows, free tiers and descriptors ---------------------------------------

def test_new_seed_rows_state_their_source():
    rows = [r for r in json.loads(SEED_PRICES_PATH.read_text())
            if r["usage_metric"] in NEW_METRICS]
    assert {(r["usage_metric"], r["region"]) for r in rows} == {
        (m, region) for m in NEW_METRICS for region in ("us-east-1", "global")}
    for r in rows:
        assert r["source"] == "https://aws.amazon.com/waf/pricing/"
        assert r["effective_date"] == "2026-09-24"


@pytest.mark.parametrize("metric,allowance", [
    ("WAF-BotControl-Request", 10_000_000),
    ("WAF-BotControl-Targeted-Request", 1_000_000),
    ("WAF-FraudControl-Request", 10_000),
])
def test_free_requests_apply_to_each_region(metric, allowance):
    assert FREE_ALLOWANCES[("aws", "AWSWAF", metric)] == allowance
    assert free_tier_scope("aws", "AWSWAF", metric) == REGION
    assert not is_global_metric("aws", "AWSWAF", metric)


@pytest.mark.parametrize("metric", sorted(NEW_METRICS))
@pytest.mark.parametrize("region,prefix", [("us-east-1", "USE1"), ("global", "Global")])
def test_descriptor_selects_the_product(creds, metric, region, prefix):
    captured = []
    rows = _sync(metric, region, captured)
    assert [v["region"] for v in captured] == ["" if region == "global" else region]
    assert {r.attributes["usagetype"] for r in rows} == {
        f"{prefix}-{NEW_METRICS[metric]}"}
    assert {(r.service, r.region) for r in rows} == {("AWSWAF", region)}


@pytest.mark.parametrize("metric", sorted(NEW_METRICS))
@pytest.mark.parametrize("region", ["us-east-1", "global"])
def test_live_rows_match_the_seed_rows(creds, metric, region):
    live = _sync(metric, region)
    seed = [r for r in load_seed_rows(["AWSWAF"])
            if r.usage_metric == metric and r.region == region]
    assert {r.unit for r in live} == {r.unit for r in seed}
    got, want = _tiers(live), _tiers(seed)
    assert [t[1:] for t in got] == [t[1:] for t in want]
    assert [t[0] for t in got] == pytest.approx([t[0] for t in want])
