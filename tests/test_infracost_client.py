"""Tests for the Infracost Cloud Pricing API client.

The live HTTP layer is mocked; these assert the client builds the right request,
selects auth correctly (Bearer token + org-id), parses the real Infracost GraphQL
response shape, and falls back loudly (never silently) when a credential is
present but the live sync fails.
"""

import json
from unittest.mock import patch, MagicMock

import pytest

from infra_cost_model.pricing.sources import infracost as ic


def _set_creds(monkeypatch):
    monkeypatch.setenv("INFRACOST_API_KEY", "test-token")
    monkeypatch.setenv("INFRACOST_ORG_ID", "org-123")


def _clear_creds(monkeypatch, tmp_path):
    monkeypatch.delenv("INFRACOST_API_KEY", raising=False)
    monkeypatch.delenv("INFRACOST_ORG_ID", raising=False)
    monkeypatch.setenv("INFRACOST_CONFIG_DIR", str(tmp_path))  # empty → no session


def _graphql_response(products):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"data": {"products": products}}
    resp.raise_for_status.return_value = None
    return resp


def test_auth_uses_bearer_and_org_from_env(monkeypatch):
    _set_creds(monkeypatch)
    assert ic.InfracostClient().auth_headers() == {
        "Authorization": "Bearer test-token",
        "x-infracost-org-id": "org-123",
    }


def test_auth_falls_back_to_session(monkeypatch, tmp_path):
    _clear_creds(monkeypatch, tmp_path)
    (tmp_path / "token.json").write_text(json.dumps({"access_token": "jwt-abc"}))
    (tmp_path / "user.json").write_text(json.dumps({"organizations": [{"id": "org-sess"}]}))
    assert ic.InfracostClient().auth_headers() == {
        "Authorization": "Bearer jwt-abc",
        "x-infracost-org-id": "org-sess",
    }


def test_auth_none_when_unauthenticated(monkeypatch, tmp_path):
    _clear_creds(monkeypatch, tmp_path)
    assert ic.InfracostClient().auth_headers() is None


def test_auth_none_with_token_but_no_org(monkeypatch, tmp_path):
    _clear_creds(monkeypatch, tmp_path)
    monkeypatch.setenv("INFRACOST_API_KEY", "test-token")  # token but no org
    assert ic.InfracostClient().auth_headers() is None


def test_query_prices_builds_request_and_parses(monkeypatch):
    _set_creds(monkeypatch)
    products = [{
        "productFamily": "Serverless",
        "attributes": [{"key": "group", "value": "AWS-Lambda-Requests"}],
        "prices": [{"USD": "0.0000002", "unit": "Requests",
                    "startUsageAmount": "0", "endUsageAmount": None}],
    }]
    with patch.object(ic.requests, "post", return_value=_graphql_response(products)) as post:
        rows = ic.InfracostClient().query_prices(
            service="AWSLambda", region="us-east-1", product_family="Serverless",
            attribute_filters=[{"key": "group", "value": "AWS-Lambda-Requests"}],
            purchase_option="on_demand",
        )
    _, kwargs = post.call_args
    assert post.call_args[0][0] == ic.INFRACOST_PRICING_API_URL
    assert kwargs["headers"]["Authorization"] == "Bearer test-token"
    assert kwargs["headers"]["x-infracost-org-id"] == "org-123"
    assert kwargs["json"]["variables"]["service"] == "AWSLambda"
    assert "products(filter" in kwargs["json"]["query"]
    assert len(rows) == 1
    assert rows[0]["price_usd"] == pytest.approx(2e-7)
    assert rows[0]["unit"] == "Requests"
    assert rows[0]["attributes"]["group"] == "AWS-Lambda-Requests"
    assert rows[0]["source"] == "infracost"


def test_query_prices_raises_on_graphql_errors(monkeypatch):
    _set_creds(monkeypatch)
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"errors": [{"message": "bad filter"}]}
    resp.raise_for_status.return_value = None
    with patch.object(ic.requests, "post", return_value=resp):
        with pytest.raises(RuntimeError, match="returned errors"):
            ic.InfracostClient().query_prices(service="AWSLambda", region="us-east-1")


def test_query_prices_raises_without_auth(monkeypatch, tmp_path):
    _clear_creds(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="auth not found"):
        ic.InfracostClient().query_prices(service="AWSLambda", region="us-east-1")


def test_sync_to_cache_filters_by_unit_and_upserts(monkeypatch):
    _set_creds(monkeypatch)
    products = [{
        "productFamily": "Serverless",
        "attributes": [],
        "prices": [
            {"USD": "0.0000002", "unit": "Requests", "startUsageAmount": "0", "endUsageAmount": None},
            {"USD": "0.5", "unit": "WRONG-UNIT", "startUsageAmount": "0", "endUsageAmount": None},
        ],
    }]
    upserted = []
    cache = MagicMock()
    cache.upsert.side_effect = lambda p: upserted.append(p)
    with patch.object(ic.requests, "post", return_value=_graphql_response(products)):
        n = ic.InfracostClient().sync_to_cache(cache, "Lambda-Request", "us-east-1")
    assert n == 1
    assert upserted[0].usage_metric == "Lambda-Request"
    assert upserted[0].source == "infracost"


def test_sync_to_cache_unknown_metric_raises(monkeypatch):
    _set_creds(monkeypatch)
    with pytest.raises(KeyError, match="No Infracost descriptor"):
        ic.InfracostClient().sync_to_cache(MagicMock(), "Totally-Unknown-Metric", "us-east-1")


def test_loud_fallback_when_authed_but_live_empty(monkeypatch):
    """Credential present but live returns nothing → warn, don't silently seed."""
    _set_creds(monkeypatch)
    with patch.object(ic.requests, "post", return_value=_graphql_response([])), \
         patch.object(ic, "_sync_fallback", return_value=(14, "seed-pricelist")):
        with pytest.warns(UserWarning, match="returned no"):
            count, source = ic.sync_pricing_catalog()
    assert source == "seed-pricelist"


def test_no_warning_when_unauthenticated(monkeypatch, tmp_path):
    """No credential → seed fallback is expected, no warning."""
    _clear_creds(monkeypatch, tmp_path)
    import warnings
    with patch.object(ic, "_sync_fallback", return_value=(14, "seed-pricelist")):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            count, source = ic.sync_pricing_catalog()
    assert source == "seed-pricelist"


# --- Descriptors added for #208-#211 handlers (issue #217) ---------------------

@pytest.mark.parametrize("metric,service,unit", [
    ("KMS-Key-Month", "awskms", "Keys"),
    ("KMS-API-Request", "awskms", "Requests"),
    ("IPv4-InUse-Hours", "AmazonVPC", "Hrs"),
    ("IPv4-Idle-Hours", "AmazonVPC", "Hrs"),
    ("CloudWatch-Metric-Month", "AmazonCloudWatch", "Metrics"),
    ("CloudWatch-Alarm-Month", "AmazonCloudWatch", "Alarms"),
    ("CloudWatch-GetMetricData", "AmazonCloudWatch", "Metrics"),
])
def test_new_handler_descriptors_present(metric, service, unit):
    d = ic.METRIC_DESCRIPTORS[metric]
    assert d["service"] == service
    assert d["unit"] == unit


def test_ipv4_descriptor_resolves_region_prefix(monkeypatch):
    """The EIP usagetype filter must have REGION_PREFIX resolved to the region's
    short code (us-east-1 -> USE1) before the query is sent."""
    _set_creds(monkeypatch)
    d = ic.METRIC_DESCRIPTORS["IPv4-InUse-Hours"]
    with patch.object(ic.requests, "post", return_value=_graphql_response([])) as post:
        ic.InfracostClient().query_prices(
            service=d["service"], region="us-east-1",
            attribute_filters=d["attribute_filters"],
        )
    sent = post.call_args.kwargs["json"]["variables"]["attributeFilters"]
    assert sent == [{"key": "usagetype", "value": "USE1-PublicIPv4:InUseAddress"}]


def test_sync_to_cache_kms_key_month_upserts(monkeypatch):
    """End-to-end (mocked HTTP): the KMS-Key-Month descriptor stores the fetched
    price under the catalog usage_metric name with source=infracost."""
    _set_creds(monkeypatch)
    products = [{
        "productFamily": "Encryption Key",
        "attributes": [{"key": "usagetype", "value": "us-east-1-KMS-Keys"}],
        "prices": [
            {"USD": "1.0", "unit": "Keys", "startUsageAmount": "0", "endUsageAmount": None},
            {"USD": "9.99", "unit": "WRONG-UNIT", "startUsageAmount": "0", "endUsageAmount": None},
        ],
    }]
    upserted = []
    cache = MagicMock()
    cache.upsert.side_effect = lambda p: upserted.append(p)
    with patch.object(ic.requests, "post", return_value=_graphql_response(products)):
        n = ic.InfracostClient().sync_to_cache(cache, "KMS-Key-Month", "us-east-1")
    assert n == 1  # WRONG-UNIT row filtered out by the descriptor's unit
    assert upserted[0].usage_metric == "KMS-Key-Month"
    assert upserted[0].service == "awskms"
    assert upserted[0].price_usd == pytest.approx(1.0)
    assert upserted[0].source == "infracost"


# --- Regionless / region-pair data-transfer sync -------------------------------

def _dt_product(usagetype, usd, transfer_type="InterRegion Outbound", unit="GB"):
    return {
        "productFamily": "",
        "attributes": [
            {"key": "usagetype", "value": usagetype},
            {"key": "transferType", "value": transfer_type},
        ],
        # Data-transfer products are catalogued globally (region="").
        "prices": [{"USD": str(usd), "unit": unit,
                    "startUsageAmount": "0", "endUsageAmount": None}],
    }


def test_data_transfer_queries_global_region(monkeypatch):
    """A `query_region: ""` descriptor must query the global catalogue even though
    the price is stored under the caller's region."""
    _set_creds(monkeypatch)
    cache = MagicMock()
    with patch.object(ic.requests, "post",
                      return_value=_graphql_response([_dt_product("USE1-USW2-AWS-Out-Bytes", 0.02)])) as post:
        ic.InfracostClient().sync_to_cache(cache, "DataTransfer-InterRegion-GB", "us-east-1")
    assert post.call_args.kwargs["json"]["variables"]["region"] == ""


def test_data_transfer_collapses_pairs_to_modal_rate(monkeypatch):
    """Only outbound-from-us-east-1 (USE1-…-AWS-Out-Bytes) non-zero rows count;
    the modal rate is stored once, flat, under the caller's region."""
    _set_creds(monkeypatch)
    products = [
        _dt_product("USE1-USW2-AWS-Out-Bytes", 0.02),   # candidate
        _dt_product("USE1-EUW1-AWS-Out-Bytes", 0.02),   # candidate (modal → 2x $0.02)
        _dt_product("USE1-APS4-AWS-Out-Bytes", 0.09),   # candidate (rarer rate)
        _dt_product("USE1-SCL1-AWS-Out-Bytes", 0.0),    # excluded: free ($0)
        _dt_product("USW2-USE1-AWS-Out-Bytes", 0.02),   # excluded: leaves us-west-2
        _dt_product("USE1-USW2-AWS-In-Bytes", 0.01),    # excluded: inbound (wrong suffix)
    ]
    upserted = []
    cache = MagicMock()
    cache.upsert.side_effect = lambda p: upserted.append(p)
    with patch.object(ic.requests, "post", return_value=_graphql_response(products)):
        n = ic.InfracostClient().sync_to_cache(cache, "DataTransfer-InterRegion-GB", "us-east-1")
    assert n == 1
    p = upserted[0]
    assert p.usage_metric == "DataTransfer-InterRegion-GB"
    assert p.region == "us-east-1"                 # stored under the sync region, not ""
    assert p.price_usd == pytest.approx(0.02)      # modal rate
    assert p.start_usage_amount is None            # flat, not tiered
    assert p.source == "infracost"


def test_data_transfer_no_matching_pairs_upserts_nothing(monkeypatch):
    """No outbound rows for the sync region → nothing stored (graceful)."""
    _set_creds(monkeypatch)
    products = [_dt_product("USW2-USE1-AWS-Out-Bytes", 0.02)]  # us-west-2 source only
    cache = MagicMock()
    with patch.object(ic.requests, "post", return_value=_graphql_response(products)):
        n = ic.InfracostClient().sync_to_cache(cache, "DataTransfer-InterRegion-GB", "us-east-1")
    assert n == 0
    cache.upsert.assert_not_called()


def test_data_transfer_descriptor_present():
    d = ic.METRIC_DESCRIPTORS["DataTransfer-InterRegion-GB"]
    assert d["service"] == "AWSDataTransfer"
    assert d["query_region"] == ""
    assert d["region_pair_source"] is True
    assert d["unit"] == "GB"


def test_data_transfer_modal_tie_breaks_to_lower_rate(monkeypatch):
    """When two rates share the top frequency, the lower rate wins (documented
    tie-break so the representative is deterministic)."""
    _set_creds(monkeypatch)
    products = [
        _dt_product("USE1-USW2-AWS-Out-Bytes", 0.09),
        _dt_product("USE1-EUW1-AWS-Out-Bytes", 0.09),
        _dt_product("USE1-APS4-AWS-Out-Bytes", 0.02),
        _dt_product("USE1-APN1-AWS-Out-Bytes", 0.02),  # 2-vs-2 tie: 0.09 and 0.02
    ]
    upserted = []
    cache = MagicMock()
    cache.upsert.side_effect = lambda p: upserted.append(p)
    with patch.object(ic.requests, "post", return_value=_graphql_response(products)):
        n = ic.InfracostClient().sync_to_cache(cache, "DataTransfer-InterRegion-GB", "us-east-1")
    assert n == 1
    assert upserted[0].price_usd == pytest.approx(0.02)  # lower of the tied rates


def test_data_transfer_unknown_region_upserts_nothing(monkeypatch):
    """An unmapped sync region resolves REGION_PREFIX to the literal fallback,
    which matches no usagetype prefix → nothing stored (falls back to seed)."""
    _set_creds(monkeypatch)
    products = [_dt_product("USE1-USW2-AWS-Out-Bytes", 0.02)]
    cache = MagicMock()
    with patch.object(ic.requests, "post", return_value=_graphql_response(products)):
        n = ic.InfracostClient().sync_to_cache(
            cache, "DataTransfer-InterRegion-GB", "moon-base-1")
    assert n == 0
    cache.upsert.assert_not_called()


# --- Regionless single-usagetype: internet egress (tiered) + inter-AZ ----------

def _dt_tiered_product(usagetype, tiers, transfer_type="AWS Outbound", unit="GB"):
    """tiers: list of (usd, start, end) — one price row each (preserves tiers)."""
    return {
        "productFamily": "",
        "attributes": [
            {"key": "usagetype", "value": usagetype},
            {"key": "transferType", "value": transfer_type},
        ],
        "prices": [{"USD": str(u), "unit": unit,
                    "startUsageAmount": str(s),
                    "endUsageAmount": (None if e is None else str(e))}
                   for (u, s, e) in tiers],
    }


def test_internet_egress_preserves_tiers_us_east_1(monkeypatch):
    """us-east-1 internet egress uses the unprefixed usagetype and keeps all tiers."""
    _set_creds(monkeypatch)
    products = [
        _dt_tiered_product("DataTransfer-Out-Bytes", [
            (0.09, 0, 10240), (0.085, 10240, 51200),
            (0.07, 51200, 153600), (0.05, 153600, None),
        ]),
        _dt_tiered_product("USW1-DataTransfer-Out-Bytes", [(0.09, 0, None)]),  # other region
        _dt_product("USE1-USW2-AWS-Out-Bytes", 0.02),                          # wrong metric
    ]
    upserted = []
    cache = MagicMock()
    cache.upsert.side_effect = lambda p: upserted.append(p)
    with patch.object(ic.requests, "post", return_value=_graphql_response(products)):
        n = ic.InfracostClient().sync_to_cache(cache, "DataTransfer-Internet-Out-GB", "us-east-1")
    assert n == 4  # four tiers of the unprefixed us-east-1 usagetype only
    assert all(p.region == "us-east-1" and p.usage_metric == "DataTransfer-Internet-Out-GB"
               for p in upserted)
    assert sorted(p.price_usd for p in upserted) == [0.05, 0.07, 0.085, 0.09]
    first = next(p for p in upserted if p.start_usage_amount == 0)
    assert first.price_usd == pytest.approx(0.09) and first.end_usage_amount == pytest.approx(10240)


def test_internet_egress_prefixes_non_us_east_1(monkeypatch):
    """A non-us-east-1 region selects the prefixed usagetype, not the bare one."""
    _set_creds(monkeypatch)
    products = [
        _dt_tiered_product("DataTransfer-Out-Bytes", [(0.09, 0, None)]),         # us-east-1 bare
        _dt_tiered_product("USW1-DataTransfer-Out-Bytes", [(0.09, 0, None)]),    # us-west-1
    ]
    upserted = []
    cache = MagicMock()
    cache.upsert.side_effect = lambda p: upserted.append(p)
    with patch.object(ic.requests, "post", return_value=_graphql_response(products)):
        n = ic.InfracostClient().sync_to_cache(cache, "DataTransfer-Internet-Out-GB", "us-west-1")
    assert n == 1
    assert upserted[0].region == "us-west-1"
    assert upserted[0].attributes["usagetype"] == "USW1-DataTransfer-Out-Bytes"


def test_inter_az_flat_rate_us_east_1(monkeypatch):
    """Inter-AZ is a single flat $0.01/GB row under the unprefixed usagetype."""
    _set_creds(monkeypatch)
    products = [
        _dt_product("DataTransfer-Regional-Bytes", 0.01, transfer_type="IntraRegion"),
        _dt_product("APS4-DataTransfer-Regional-Bytes", 0.01, transfer_type="IntraRegion"),
    ]
    upserted = []
    cache = MagicMock()
    cache.upsert.side_effect = lambda p: upserted.append(p)
    with patch.object(ic.requests, "post", return_value=_graphql_response(products)):
        n = ic.InfracostClient().sync_to_cache(cache, "DataTransfer-InterAZ-GB", "us-east-1")
    assert n == 1
    assert upserted[0].price_usd == pytest.approx(0.01)
    assert upserted[0].region == "us-east-1"


def test_data_transfer_internet_and_interaz_descriptors_present():
    d1 = ic.METRIC_DESCRIPTORS["DataTransfer-Internet-Out-GB"]
    assert d1["query_region"] == "" and d1["regionless_usagetype"] is True
    assert d1["usagetype_base"] == "DataTransfer-Out-Bytes"
    d2 = ic.METRIC_DESCRIPTORS["DataTransfer-InterAZ-GB"]
    assert d2["regionless_usagetype"] is True
    assert d2["usagetype_base"] == "DataTransfer-Regional-Bytes"


def test_regionless_usagetype_queries_global_region(monkeypatch):
    """Internet-egress / inter-AZ descriptors must issue the global (region="") query."""
    _set_creds(monkeypatch)
    cache = MagicMock()
    prod = _dt_tiered_product("DataTransfer-Out-Bytes", [(0.09, 0, None)])
    with patch.object(ic.requests, "post", return_value=_graphql_response([prod])) as post:
        ic.InfracostClient().sync_to_cache(cache, "DataTransfer-Internet-Out-GB", "us-east-1")
    assert post.call_args.kwargs["json"]["variables"]["region"] == ""


def test_regionless_usagetype_unknown_region_upserts_nothing(monkeypatch):
    """An unmapped region resolves to the 'REGION_PREFIX' fallback target, which
    matches no usagetype → nothing stored (falls back to seed)."""
    _set_creds(monkeypatch)
    products = [_dt_tiered_product("DataTransfer-Out-Bytes", [(0.09, 0, None)])]
    cache = MagicMock()
    with patch.object(ic.requests, "post", return_value=_graphql_response(products)):
        n = ic.InfracostClient().sync_to_cache(
            cache, "DataTransfer-Internet-Out-GB", "moon-base-1")
    assert n == 0
    cache.upsert.assert_not_called()


# --- Region usagetype prefix map integrity -------------------------------------

def test_region_usagetype_prefixes_are_unique():
    """Each region must map to a DISTINCT usagetype prefix — a collision makes two
    regions' prices indistinguishable in the usagetype filters (e.g. the data-
    transfer region-pair / regionless sync)."""
    from collections import Counter
    from infra_cost_model.pricing.sources.infracost import _REGION_PREFIX
    dupes = {v: n for v, n in Counter(_REGION_PREFIX.values()).items() if n > 1}
    assert not dupes, f"duplicate region usagetype prefixes: {dupes}"


def test_ap_region_prefixes_match_aws_codes():
    """AWS assigns usage-type region codes by launch order, not name."""
    from infra_cost_model.pricing.sources.infracost import _REGION_PREFIX
    assert _REGION_PREFIX["ap-south-1"] == "APS3"      # Mumbai
    assert _REGION_PREFIX["ap-southeast-3"] == "APS4"  # Jakarta
    assert _REGION_PREFIX["ap-south-2"] == "APS5"      # Hyderabad


# --- Multi-region live sync ----------------------------------------------------

def test_sync_pricing_catalog_covers_multiple_regions(monkeypatch):
    """sync_pricing_catalog fetches every metric for EACH requested region."""
    _set_creds(monkeypatch)
    calls = []
    monkeypatch.setattr(ic.InfracostClient, "is_authenticated", lambda self: True)
    monkeypatch.setattr(
        ic.InfracostClient, "sync_to_cache",
        lambda self, cache, usage_metric, region, vendor="aws":
            calls.append((usage_metric, region)) or 1,
    )
    total, source = ic.sync_pricing_catalog(
        services=["KMS-Key-Month"], regions=["us-east-1", "eu-west-1", "ap-south-1"])
    assert source == "infracost"
    assert total == 3
    assert {r for _, r in calls} == {"us-east-1", "eu-west-1", "ap-south-1"}


def test_sync_pricing_catalog_defaults_to_us_east_1(monkeypatch):
    """No regions arg → us-east-1 only (backward compatible)."""
    _set_creds(monkeypatch)
    calls = []
    monkeypatch.setattr(ic.InfracostClient, "is_authenticated", lambda self: True)
    monkeypatch.setattr(
        ic.InfracostClient, "sync_to_cache",
        lambda self, cache, usage_metric, region, vendor="aws":
            calls.append(region) or 1,
    )
    ic.sync_pricing_catalog(services=["KMS-Key-Month"])
    assert calls == ["us-east-1"]


# --- NAT Gateway (priced under AmazonEC2, stored under AmazonVPC) ---------------

def _nat_product(usagetype, usd, unit):
    return {
        "productFamily": "NAT Gateway",
        "attributes": [
            {"key": "usagetype", "value": usagetype},
            {"key": "operation", "value": "NatGateway"},
        ],
        "prices": [{"USD": str(usd), "unit": unit,
                    "startUsageAmount": "0", "endUsageAmount": None}],
    }


def test_nat_gateway_descriptors_present():
    for m in ("NAT-Gateway-Hour", "NAT-Gateway-DataProcessed"):
        d = ic.METRIC_DESCRIPTORS[m]
        assert d["service"] == "AmazonEC2"
        assert d["store_service"] == "AmazonVPC"
        assert d["product_family"] == "NAT Gateway"


def test_nat_hour_remaps_service_to_vpc(monkeypatch):
    _set_creds(monkeypatch)
    products = [_nat_product("NatGateway-Hours", 0.045, "Hrs")]
    upserted = []
    cache = MagicMock()
    cache.upsert.side_effect = lambda p: upserted.append(p)
    with patch.object(ic.requests, "post", return_value=_graphql_response(products)):
        n = ic.InfracostClient().sync_to_cache(cache, "NAT-Gateway-Hour", "us-east-1")
    assert n == 1
    # Queried under AmazonEC2 but stored under the service the handler models it as.
    assert upserted[0].service == "AmazonVPC"
    assert upserted[0].usage_metric == "NAT-Gateway-Hour"
    assert upserted[0].price_usd == pytest.approx(0.045)


def test_nat_data_processed_excludes_prvd(monkeypatch):
    """The $0 provisioned-throughput 'Prvd' row shares the GB unit and must be
    excluded so it doesn't dilute the data-processed rate."""
    _set_creds(monkeypatch)
    products = [
        _nat_product("NatGateway-Bytes", 0.045, "GB"),
        _nat_product("NatGateway-Prvd-Bytes", 0.0, "GB"),  # excluded
    ]
    upserted = []
    cache = MagicMock()
    cache.upsert.side_effect = lambda p: upserted.append(p)
    with patch.object(ic.requests, "post", return_value=_graphql_response(products)):
        n = ic.InfracostClient().sync_to_cache(cache, "NAT-Gateway-DataProcessed", "us-east-1")
    assert n == 1
    assert upserted[0].service == "AmazonVPC"
    assert upserted[0].price_usd == pytest.approx(0.045)
