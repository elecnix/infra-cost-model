"""Route 53 queries are global, and a CloudFront web ACL bills from the global rows.

#384: the AWS price list states public hosted-zone queries as the
``DNS-Queries`` product, location "Any", routing type "Standard": $0.40 per
million for the first billion queries a month and $0.20 after. Each region's
``USE1-DNS-Queries`` product is Route 53 Resolver queries ("$0.40 per
1,000,000 resolver queries"), which a hosted zone doesn't bill. The account's
queries in every region share the one billion query tier.

#385: a web ACL with the scope CLOUDFRONT bills from the ``Global-`` products
(location "Any"), and a regional web ACL from its region's products. The
handler gives a CloudFront web ACL the region "global", as the CloudFront
handler does for a distribution, so all of them share one pool.

The fake Cloud Pricing API below holds the products that the live API
returned on 2026-09-24.
"""

import json
import warnings
from unittest.mock import MagicMock, patch

import pytest

from infra_cost_model.engine import CostEngine
from infra_cost_model.pricing.cache import SEED_PRICES_PATH, load_seed_rows
from infra_cost_model.pricing.catalog import PricingCatalog
from infra_cost_model.pricing.global_services import is_global_metric
from infra_cost_model.pricing.sources import infracost as ic
from infra_cost_model.resources.waf import WAFv2WebACL

INF = "Inf"


def _product(region, family, usagetype, prices):
    return {
        "region": region, "productFamily": family,
        "attributes": [{"key": "usagetype", "value": usagetype}],
        "prices": [{"USD": usd, "unit": unit, "startUsageAmount": start,
                    "endUsageAmount": end} for usd, unit, start, end in prices],
    }


QUERY_TIERS = [("0.0000004", "Queries", "0", "1000000000"),
               ("0.0000002", "Queries", "1000000000", INF)]
WAF = "Web Application Firewall"

CATALOGUE = [
    _product("", "DNS Query", "DNS-Queries", QUERY_TIERS),
    _product("", "DNS Query", "LBR-Queries",
             [("0.0000006", "Queries", "0", "1000000000"),
              ("0.0000003", "Queries", "1000000000", INF)]),
    _product("us-east-1", "DNS Query", "USE1-DNS-Queries", QUERY_TIERS),
    _product("eu-west-1", "DNS Query", "EU-DNS-Queries", QUERY_TIERS),
    _product("", WAF, "Global-WebACLV2", [("5", "Month", "0", INF)]),
    _product("", WAF, "Global-RuleV2", [("1", "Month", "0", INF)]),
    _product("", WAF, "Global-RequestV2-Tier1", [("0.0000006", "Request", "0", INF)]),
    _product("", WAF, "Global-ShieldProtected-WebACLV2", [("0", "Month", "0", INF)]),
    _product("us-east-1", WAF, "USE1-WebACLV2", [("5", "Month", "0", INF)]),
    _product("us-east-1", WAF, "USE1-RuleV2", [("1", "Month", "0", INF)]),
    _product("us-east-1", WAF, "USE1-RequestV2-Tier1",
             [("0.0000006", "Request", "0", INF)]),
]


def _fake_post(captured=None):
    def post(url, headers=None, json=None, timeout=None):
        variables = json["variables"]
        if captured is not None:
            captured.append(variables)
        family = variables.get("productFamily")
        filters = variables.get("attributeFilters") or []
        matched = []
        for product in CATALOGUE:
            attrs = {a["key"]: a["value"] for a in product["attributes"]}
            if product["region"] != variables["region"]:
                continue
            if family and product["productFamily"] != family:
                continue
            if all(attrs.get(f["key"], "") == f["value"] for f in filters):
                matched.append(product)
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


# --- #384: Route 53 queries ------------------------------------------------------

def test_seed_states_the_20_cent_tier_after_one_billion_queries(seed_catalog):
    # 1,500 million queries: 1,000 million at $0.40 and 500 million at $0.20.
    got = seed_catalog.query("aws", "AmazonRoute53", "us-east-1",
                             "Route53-Query", 1500).total_cost
    assert got == pytest.approx(1000 * 0.40 + 500 * 0.20)


def test_new_seed_query_rows_state_their_source():
    rows = [r for r in json.loads(SEED_PRICES_PATH.read_text())
            if r["usage_metric"] == "Route53-Query"]
    assert len(rows) == 2
    for r in rows:
        assert r["source"] == "https://aws.amazon.com/route53/pricing/"
        assert r["effective_date"] == "2026-09-24"


@pytest.mark.parametrize("region", ["us-east-1", "eu-west-1"])
def test_query_descriptor_selects_the_global_hosted_zone_product(creds, region):
    captured = []
    rows = _sync("Route53-Query", region, captured)
    assert [v["region"] for v in captured] == [""]
    assert {r.attributes["usagetype"] for r in rows} == {"DNS-Queries"}
    assert {(r.service, r.region, r.unit) for r in rows} == {
        ("AmazonRoute53", region, "1M-Queries")}
    assert _tiers(rows) == [(0.2, 1000.0, None), (0.4, 0.0, 1000.0)]


def test_live_query_rows_match_the_seed_rows(creds):
    live = _sync("Route53-Query", "us-east-1")
    seed = [r for r in load_seed_rows(["AmazonRoute53"])
            if r.usage_metric == "Route53-Query" and r.region == "us-east-1"]
    assert {r.unit for r in live} == {r.unit for r in seed}
    got, want = _tiers(live), _tiers(seed)
    assert [t[1:] for t in got] == [t[1:] for t in want]
    assert [t[0] for t in got] == pytest.approx([t[0] for t in want])


def test_route53_queries_are_a_global_metric():
    assert is_global_metric("aws", "AmazonRoute53", "Route53-Query")


def _zone(region, millions):
    return {
        "nodeType": "storage", "resourceAddress": f"aws_route53_zone.{region}",
        "provider": "aws", "service": "AmazonRoute53", "region": region,
        "usageMetrics": {"queries": {"unit": "1M-Queries", "value": millions,
                                     "fixed": True}},
    }


def test_two_regions_share_the_one_billion_query_tier(seed_catalog):
    # 800 + 800 million queries: 1,000 million at $0.40 and 600 million at $0.20.
    costs = _monthly(seed_catalog, {"us": _zone("us-east-1", 800),
                                    "eu": _zone("eu-west-1", 800)})
    assert costs["us"] + costs["eu"] == pytest.approx(400 + 120)


# --- #385: WAF web ACL scope ------------------------------------------------------

@pytest.mark.parametrize("extract,resource", [
    (WAFv2WebACL.extract_tf, {"address": "aws_wafv2_web_acl.edge",
                              "values": {"scope": "CLOUDFRONT", "region": "us-east-1"}}),
    (WAFv2WebACL.extract_pulumi, {"id": "aws.wafv2.WebAcl:edge",
                                  "inputs": {"scope": "CLOUDFRONT", "region": "us-east-1"}}),
    (WAFv2WebACL.extract_cdk, {"LogicalId": "EdgeAcl",
                               "Properties": {"Scope": "CLOUDFRONT"}}),
])
def test_cloudfront_web_acl_has_the_global_region(extract, resource):
    result = extract(resource)
    assert result.region == "global"
    assert result.config["scope"] == "CLOUDFRONT"


def test_regional_web_acl_keeps_its_region():
    result = WAFv2WebACL.extract_tf({"address": "aws_wafv2_web_acl.api", "values": {
        "scope": "REGIONAL", "region": "eu-west-1"}})
    assert result.region == "eu-west-1"


@pytest.mark.parametrize("metric,usagetype,price", [
    ("WAF-WebACL-Month", "Global-WebACLV2", 5.0),
    ("WAF-Rule-Month", "Global-RuleV2", 1.0),
    ("WAF-Request", "Global-RequestV2-Tier1", 0.0000006),
])
def test_waf_descriptors_store_the_global_rows_under_global(creds, metric,
                                                            usagetype, price):
    captured = []
    rows = _sync(metric, "global", captured)
    assert [v["region"] for v in captured] == [""]
    assert [r.attributes["usagetype"] for r in rows] == [usagetype]
    assert {(r.service, r.region) for r in rows} == {("AWSWAF", "global")}
    assert rows[0].price_usd == pytest.approx(price)


def test_waf_descriptors_still_select_the_region_rows(creds):
    rows = _sync("WAF-WebACL-Month", "us-east-1")
    assert [r.attributes["usagetype"] for r in rows] == ["USE1-WebACLV2"]


def test_aws_sync_regions_include_global():
    assert "global" in ic.sync_regions("aws")
    assert "global" not in ic.sync_regions("gcp")


def test_global_sync_region_only_runs_the_global_descriptors(creds, monkeypatch):
    calls = []

    def fake_sync(self, cache, metric, region, vendor="aws"):
        calls.append((metric, region))
        return 1

    monkeypatch.setattr(ic.InfracostClient, "sync_to_cache", fake_sync)
    monkeypatch.setattr("infra_cost_model.pricing.cache.PricingCache",
                        lambda *a, **k: MagicMock())
    ic.sync_pricing_catalog(vendor="aws", regions=["global", "us-east-1"])
    synced_global = {m for m, r in calls if r == "global"}
    assert synced_global == {"WAF-WebACL-Month", "WAF-Rule-Month", "WAF-Request"}
    assert "Lambda-Request" in {m for m, r in calls if r == "us-east-1"}


def _acl(region, acls=1, rules=2, requests=1_000_000):
    return {
        "nodeType": "routing", "resourceAddress": f"aws_wafv2_web_acl.{region}",
        "provider": "aws", "service": "AWSWAF", "region": region,
        "usageMetrics": {
            "webAcls": {"unit": "Months", "value": acls, "fixed": True},
            "rules": {"unit": "Rules", "value": rules, "fixed": True},
            "requests": {"unit": "requests", "value": requests, "fixed": True},
        },
    }


def test_seed_prices_a_cloudfront_web_acl(seed_catalog):
    costs = _monthly(seed_catalog, {"edge": _acl("global")})
    assert costs["edge"] == pytest.approx(5 + 2 * 1 + 0.60)


@pytest.mark.parametrize("metric", ["WAF-WebACL-Month", "WAF-Rule-Month", "WAF-Request"])
@pytest.mark.parametrize("region", ["us-east-1", "global"])
def test_live_waf_rows_have_the_seed_unit(creds, metric, region):
    seed = {r.unit for r in load_seed_rows(["AWSWAF"])
            if r.usage_metric == metric and r.region == region}
    assert seed
    assert {r.unit for r in _sync(metric, region)} == seed
