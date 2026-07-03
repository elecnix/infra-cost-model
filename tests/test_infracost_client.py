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
