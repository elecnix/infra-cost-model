"""The descriptor DSL declares its field set and validates it at import (#422).

``METRIC_DESCRIPTORS`` maps every catalog metric a live sync can price to the
Infracost product query that prices it. Each entry is a plain dict, read by
string literal, so nothing until now said which keys exist: a misspelled one
was dropped, the query went out without the filter the descriptor meant, the
metric stored no rows, and the engine read those absent rows as a $0 price.

These tests check that ``DESCRIPTOR_FIELDS`` is the declaration, that
``validate_descriptors`` catches what used to pass silently, and that the sync
is the three steps ``parse_descriptor`` -> ``_fetch_prices`` -> ``_store_rows``.
"""

from unittest.mock import MagicMock, patch

import pytest

from infra_cost_model.pricing.sources import infracost as ic


def _descriptor(**fields) -> dict:
    """A valid descriptor with *fields* overridden, to break one rule."""
    return {"service": "AWSLambda", "attribute_filters": [
        {"key": "usagetype", "value": "REGION_PREFIX-Request"}], "unit": "Requests",
        **fields}


def _problems(**fields) -> str:
    with pytest.raises(ic.DescriptorError) as exc:
        ic.validate_descriptors({"Test-Metric": _descriptor(**fields)})
    return str(exc.value)


# --- The field set is declared, and the shipped descriptors satisfy it -------


def test_every_shipped_descriptor_validates():
    """Importing the module runs this check; assert it again against the table."""
    assert ic.validate_descriptors() == len(ic.METRIC_DESCRIPTORS)
    assert len(ic.METRIC_DESCRIPTORS) > 200


def test_every_field_a_shipped_descriptor_uses_is_declared():
    """The declaration and the descriptors agree, in both directions."""
    used = {name for d in ic.METRIC_DESCRIPTORS.values() for name in d}
    assert used <= set(ic.DESCRIPTOR_FIELDS), used - set(ic.DESCRIPTOR_FIELDS)


def test_every_declared_field_is_used_by_some_descriptor():
    """A declared field no descriptor uses is a rule nothing can break."""
    used = {name for d in ic.METRIC_DESCRIPTORS.values() for name in d}
    assert set(ic.DESCRIPTOR_FIELDS) - used == set()


def test_every_declared_field_states_what_it_does():
    assert [n for n, f in ic.DESCRIPTOR_FIELDS.items() if not f.doc] == []


def test_service_is_the_only_required_field():
    assert [n for n, f in ic.DESCRIPTOR_FIELDS.items() if f.required] == ["service"]
    assert all("service" in d for d in ic.METRIC_DESCRIPTORS.values())


# --- A misspelled field fails loudly, with the real name ----------------------


@pytest.mark.parametrize("typo,real", [
    ("store_servce", "store_service"),
    ("store_ervice", "store_service"),
    ("unit_scalee", "unit_scale"),
    ("attribute_filter", "attribute_filters"),
    ("atribute_patterns", "attribute_patterns"),
    ("product_familly", "product_family"),
    ("usagetype_basee", "usagetype_base"),
    ("reginaless_usagetype", "regionless_usagetype"),
    ("vendr", "vendor"),
])
def test_a_misspelled_field_name_is_rejected_with_the_real_name(typo, real):
    message = _problems(**{typo: "x"})
    assert f"'{typo}' is not a descriptor field" in message
    assert f"did you mean '{real}'" in message


def test_a_field_name_removed_from_the_dsl_is_rejected():
    """The rule is not a list of the keys in use today; a removed key is unknown."""
    message = _problems(service="AWSLambda", retired_field="x")
    assert "'retired_field' is not a descriptor field" in message


def test_the_error_names_the_descriptor_and_lists_the_field_set():
    message = _problems(store_servce="AWSLambda")
    assert "Test-Metric" in message
    for name in ic.DESCRIPTOR_FIELDS:
        assert name in message


def test_every_bad_descriptor_is_reported_at_once():
    """One import error must not cost one round trip per descriptor."""
    with pytest.raises(ic.DescriptorError) as exc:
        ic.validate_descriptors({
            "A-Metric": _descriptor(store_servce="X"),
            "B-Metric": _descriptor(unit_scalee=2),
            "C-Metric": _descriptor(unit_scale="1000"),
        })
    message = str(exc.value)
    assert "3 problem(s)" in message
    for metric in ("A-Metric", "B-Metric", "C-Metric"):
        assert metric in message


def test_an_unknown_field_is_rejected_before_the_value_is_read():
    """A typo on a field that has no type check is still rejected."""
    assert "'made_up' is not a descriptor field" in _problems(made_up=object())


# --- A declared field rejects the wrong value ---------------------------------


@pytest.mark.parametrize("field,bad", [
    ("service", ""),
    ("service", 42),
    ("store_service", ""),
    ("store_unit", ["requests"]),
    ("product_family", ""),
    ("vendor", "AWS"),
    ("vendor", "gcp2"),
    ("purchase_option", ""),
    ("unit", ""),
    ("unit", []),
    ("unit", ["Requests", ""]),
    ("unit", 10),
    ("unit_scale", 0),
    ("unit_scale", -1),
    ("unit_scale", "10000"),
    ("unit_scale", True),
    ("global_scope", "yes"),
    ("unprefixed_in_us_east_1", 1),
    ("regionless_usagetype", "true"),
    ("region_pair_source", "true"),
    ("azure_retail", "true"),
    ("usagetype_base", ""),
    ("usagetype_suffix", ""),
    ("usagetype_exclude", []),
    ("usagetype_exclude", "Prvd"),
    ("usagetype_exclude", [1]),
    ("attribute_filters", []),
    ("attribute_filters", [{"key": "usagetype"}]),
    ("attribute_filters", [{"key": "usagetype", "value": ""}]),
    ("attribute_filters", [{"key": "usagetype", "value": "x", "extra": "y"}]),
    ("attribute_filters", {"key": "usagetype", "value": "x"}),
    ("attribute_patterns", {}),
    ("attribute_patterns", {"description": "["}),
    ("attribute_patterns", {"description": 1}),
    ("attribute_patterns", {"": "x"}),
])
def test_a_field_rejects_a_value_of_the_wrong_shape(field, bad):
    assert field in _problems(**{field: bad})


def test_a_missing_required_field_is_rejected():
    with pytest.raises(ic.DescriptorError, match="'service' is required"):
        ic.validate_descriptors({"Test-Metric": {"unit": "Requests"}})


def test_a_descriptor_that_is_not_a_dict_is_rejected():
    with pytest.raises(ic.DescriptorError, match="expected a dict"):
        ic.validate_descriptors({"Test-Metric": ["service"]})


# --- Cross-field rules: a rule one field cannot state alone -------------------


def test_a_scaled_descriptor_must_name_its_stored_unit():
    """A row priced per unit that keeps the API's block unit charges 10,000x."""
    message = _problems(unit_scale=10_000)
    assert "'unit_scale' without 'store_unit'" in message


def test_a_scaled_descriptor_with_a_stored_unit_is_accepted():
    ic.validate_descriptors({"Test-Metric": _descriptor(
        unit_scale=10_000, store_unit="requests")})


def test_a_scale_of_one_needs_no_stored_unit():
    ic.validate_descriptors({"Test-Metric": _descriptor(unit_scale=1)})


def test_a_regionless_usagetype_needs_its_base():
    assert "'regionless_usagetype' needs 'usagetype_base'" in _problems(
        regionless_usagetype=True)


def test_a_descriptor_selects_rows_one_way_only():
    """region_pair_source and regionless_usagetype name different rows."""
    assert "may name only one" in _problems(
        region_pair_source=True, regionless_usagetype=True, usagetype_base="X")


def test_the_query_region_of_the_global_catalogue_is_allowed():
    """``query_region: ""`` reads the global catalogue; it is not an empty field."""
    ic.validate_descriptors({"Test-Metric": _descriptor(query_region="")})


# --- The parse step: a descriptor resolved for one region --------------------


def test_parse_resolves_the_vendor_service_and_store_names():
    query = ic.parse_descriptor("NAT-Gateway-Hour", "us-east-1")
    assert (query.service, query.store_service) == ("AmazonEC2", "AmazonVPC")
    assert (query.vendor, query.mode) == ("aws", "one_product")


def test_parse_defaults_the_vendor_to_the_callers():
    """A descriptor that names no cloud is priced in the caller's."""
    assert "vendor" not in ic.METRIC_DESCRIPTORS["NAT-Gateway-Hour"]
    assert ic.parse_descriptor(
        "NAT-Gateway-Hour", "us-east-1", vendor="azure").vendor == "azure"


def test_parse_lets_a_descriptor_override_the_callers_vendor():
    """Azure and GCP descriptors name their own cloud, whatever the caller says."""
    assert ic.parse_descriptor(
        "CosmosDB-Serverless-RU", "eastus", vendor="aws").vendor == "azure"


def test_parse_resolves_the_global_usagetype_prefix():
    query = ic.parse_descriptor("WAF-WebACL-Month", ic.GLOBAL_REGION)
    assert query.query_region == ""
    assert query.attribute_filters == [
        {"key": "usagetype", "value": "Global-WebACLV2"}]


def test_parse_drops_the_region_prefix_in_us_east_1_when_the_descriptor_says_so():
    query = ic.parse_descriptor("ALB-Hour", "us-east-1")
    assert {"key": "usagetype", "value": "LoadBalancerUsage"} \
        in query.attribute_filters


def test_parse_keeps_the_prefix_in_every_other_region():
    query = ic.parse_descriptor("ALB-Hour", "eu-west-1")
    assert {"key": "usagetype", "value": "REGION_PREFIX-LoadBalancerUsage"} \
        in query.attribute_filters


def test_parse_names_the_selector_the_descriptor_asks_for():
    assert ic.parse_descriptor("Lambda-Request", "us-east-1").mode == "one_product"
    assert ic.parse_descriptor(
        "DataTransfer-InterRegion-GB", "us-east-1").mode == "region_pair"
    assert ic.parse_descriptor(
        "DataTransfer-Internet-Out-GB", "us-east-1").mode == "regionless"


def test_parse_resolves_the_cloud_run_tier_of_the_region():
    """The pattern is region-dependent, so parse settles it before selecting."""
    tier1 = ic.parse_descriptor("CloudRun-vCPU-Second", "us-central1")
    tier2 = ic.parse_descriptor("CloudRun-vCPU-Second", "europe-west2")
    assert "Tier 2" not in tier1.attribute_patterns["description"]
    assert "Tier 2" in tier2.attribute_patterns["description"]


def test_parse_of_an_unknown_metric_raises_keyerror():
    with pytest.raises(KeyError, match="No Infracost descriptor"):
        ic.parse_descriptor("Totally-Unknown-Metric", "us-east-1")


def test_parse_of_a_metric_without_a_global_product_raises():
    with pytest.raises(KeyError, match="has no global product"):
        ic.parse_descriptor("Lambda-Request", ic.GLOBAL_REGION)


# --- The transport step: substitutable without a network ----------------------


def _prices(usagetype="USE1-Request", usd="0.20", unit="Requests", **overrides):
    """One price as ``query_prices`` returns it: the shape the selectors read."""
    price = {"vendor": "aws", "service": "AWSLambda", "region": "us-east-1",
             "product_family": "Serverless", "unit": unit, "USD": usd,
             "attributes": {"usagetype": usagetype}, "price_usd": float(usd),
             "start_usage_amount": 0.0, "end_usage_amount": None,
             "source": "infracost"}
    price.update(overrides)
    return price


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("INFRACOST_API_KEY", "test-token")
    monkeypatch.setenv("INFRACOST_ORG_ID", "org-123")


def test_a_substituted_transport_feeds_the_selectors_and_the_store(creds, monkeypatch):
    """Patching _fetch_prices replaces the network and nothing else.

    The rows the fake transport returns reach the cache through the real
    selector and the real store step. DynamoDB ReadRequest is used because no
    free allowance is layered on top of the row it stores.
    """
    monkeypatch.setattr(ic.InfracostClient, "_fetch_prices",
                        lambda self, query: [_prices(
                            usagetype="USE1-ReadRequestUnits",
                            unit="ReadRequestUnits", usd="0.125")])
    upserted = []
    cache = MagicMock()
    cache.upsert.side_effect = upserted.append
    with patch.object(ic.requests, "post") as post:
        n = ic.InfracostClient().sync_to_cache(cache, "Dynamo-ReadRequest", "us-east-1")
    post.assert_not_called()
    assert (n, len(upserted)) == (1, 1)
    assert (upserted[0].vendor, upserted[0].service, upserted[0].usage_metric,
            upserted[0].unit) == ("aws", "AmazonDynamoDB", "Dynamo-ReadRequest",
                                  "ReadRequestUnits")
    assert upserted[0].price_usd == pytest.approx(0.125)


def test_the_transport_receives_the_parsed_query(creds, monkeypatch):
    """The fetch step is handed the resolved query, not the raw descriptor."""
    seen = []

    def fetch(self, query):
        seen.append(query)
        return []

    monkeypatch.setattr(ic.InfracostClient, "_fetch_prices", fetch)
    ic.InfracostClient().sync_to_cache(MagicMock(), "NAT-Gateway-Hour", "us-east-1")
    (query,) = seen
    assert (query.usage_metric, query.region, query.store_service) == (
        "NAT-Gateway-Hour", "us-east-1", "AmazonVPC")


def test_the_store_writes_the_rows_of_the_query_it_is_given(creds, monkeypatch):
    """_store_rows is callable on its own: parse, feed it prices, read the cache."""
    query = ic.parse_descriptor("NAT-Gateway-DataProcessed", "us-east-1")
    upserted = []
    cache = MagicMock()
    cache.upsert.side_effect = upserted.append
    n = ic.InfracostClient()._store_rows(cache, query, [_prices(
        usagetype="USE1-NatGateway-Hours", unit="GB", usd="0.045")])
    assert (n, len(upserted)) == (1, 1)
    assert upserted[0].service == "AmazonVPC"


def test_sync_is_the_three_steps_in_order(creds, monkeypatch):
    """parse -> fetch -> store, once each: the split adds no hidden work."""
    calls = []
    real_parse, real_fetch = ic.parse_descriptor, ic.InfracostClient._fetch_prices
    real_store = ic.InfracostClient._store_rows
    monkeypatch.setattr(ic.InfracostClient, "query_prices",
                        lambda self, **kw: [_prices()])

    monkeypatch.setattr(ic, "parse_descriptor",
                        lambda *a: (calls.append("parse"), real_parse(*a))[1])
    monkeypatch.setattr(
        ic.InfracostClient, "_fetch_prices",
        lambda self, query: (calls.append("fetch"), real_fetch(self, query))[1])
    monkeypatch.setattr(
        ic.InfracostClient, "_store_rows",
        lambda self, cache, query, prices: (calls.append("store"),
                                            real_store(self, cache, query, prices))[1])
    ic.InfracostClient().sync_to_cache(MagicMock(), "Lambda-Request", "us-east-1")
    assert calls == ["parse", "fetch", "store"]


def test_parse_does_not_touch_the_network(creds):
    """parse_descriptor is pure: it can be read in a test with no credential."""
    with patch.object(ic.requests, "post") as post, \
            patch.object(ic.requests, "get") as get:
        ic.parse_descriptor("Lambda-Request", "us-east-1")
    post.assert_not_called()
    get.assert_not_called()