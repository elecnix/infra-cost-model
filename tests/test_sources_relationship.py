"""The pricing source layer is one source with two nested fallbacks (#434).

The package used to re-export the Infracost adapter and the AWS Price List
adapter side by side, which read as two interchangeable sources. They are not
peers: ``aws_pricing`` is a callee of the Infracost adapter, and
``azure_retail`` is nested inside it. These tests pin that relationship so it
cannot quietly drift back into "two peer adapters".
"""

from unittest.mock import MagicMock

import pytest

from infra_cost_model.pricing import sources
from infra_cost_model.pricing.sources import aws_pricing, azure_retail, infracost as ic


# --- the public surface ---------------------------------------------------------


def test_the_package_names_one_source():
    """The fallbacks are not presented as peer sources of the package."""
    assert sources.__all__ == ["InfracostClient", "sync_pricing_catalog"]
    assert not hasattr(sources, "aws_fallback_prices")
    assert not hasattr(sources, "fetch_aws_price_list")


def test_the_fallbacks_stay_importable_at_their_own_module():
    """Dropping them from ``__all__`` does not put them out of reach."""
    assert callable(aws_pricing.aws_fallback_prices)
    assert callable(aws_pricing.fetch_aws_price_list)
    assert callable(azure_retail.query_azure_retail_prices)


# --- aws_pricing is a callee of the Infracost adapter ----------------------------


def test_the_infracost_module_calls_the_aws_fallback(monkeypatch):
    """The dependency points one way: infracost imports and calls aws_pricing."""
    calls = []

    def fake_fallback(services, cache, **kwargs):
        calls.append(services)
        return 7

    monkeypatch.setattr(aws_pricing, "aws_fallback_prices", fake_fallback)
    assert ic.seed_pricing_catalog(["AWSLambda"]) == (7, "seed-pricelist")
    assert ic._sync_fallback("aws", ["AWSLambda"], MagicMock()) == (7, "aws-pricelist")
    assert calls == [["AWSLambda"], ["AWSLambda"]]


def test_the_aws_fallback_declines_a_vendor_it_cannot_price():
    """A fallback that only reads AWS prices says so instead of guessing."""
    assert ic._sync_fallback("azure", None, MagicMock()) == (0, "fallback-unsupported")


# --- azure_retail is nested in the Infracost adapter -----------------------------

# The one retail item the descriptor for Bandwidth-Internet-Out-GB selects.
FILTERS = [{"key": "productName", "value": "Rtn Preference: MGN"},
           {"key": "skuName", "value": "Standard"},
           {"key": "meterName", "value": "Standard Data Transfer Out"}]

RETAIL_ITEM = {
    "productName": "Rtn Preference: MGN", "skuName": "Standard",
    "meterName": "Standard Data Transfer Out", "meterId": "m-1",
    "productId": "p-1", "serviceId": "s-1", "serviceFamily": "Bandwidth",
    "armSkuName": "", "effectiveStartDate": "2024-01-01T00:00:00Z",
    "retailPrice": "0.087", "unitOfMeasure": "1 GB", "tierMinimumUnits": "0",
}


class _Response:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


@pytest.fixture
def retail_api(monkeypatch):
    """Serve one Azure Retail item and count the calls."""
    calls = []

    def get(*args, **kwargs):
        calls.append(args)
        return _Response({"Items": [RETAIL_ITEM]})

    monkeypatch.setattr(azure_retail.requests, "get", get)
    return calls


def test_a_retail_row_carries_the_field_set_of_an_infracost_price(retail_api,
                                                                   monkeypatch):
    """Why `azure_retail` is nested in `_fetch_prices` and is not a source.

    Its rows reach the same selector and the same store as Infracost's, so the
    two must agree on the fields — the claim its docstring makes.
    """
    monkeypatch.setattr(ic.requests, "post", lambda *a, **kw: _Response(
        {"data": {"products": [{
            "productFamily": "Bandwidth",
            "attributes": [{"key": f["key"], "value": f["value"]} for f in FILTERS],
            "prices": [{"USD": "0.087", "unit": "1 GB", "startUsageAmount": "0",
                        "endUsageAmount": None}],
        }]}}))
    client = ic.InfracostClient(api_key="k", org_id="o")
    infracost_row = client.query_prices("Bandwidth", "eastus", vendor="azure")[0]
    retail_row = azure_retail.query_azure_retail_prices(
        "Bandwidth", "eastus", FILTERS)[0]
    assert set(retail_row) == set(infracost_row)
    assert retail_row["product_family"] == "Bandwidth"
    assert retail_row["attributes"]["meterName"] == "Standard Data Transfer Out"
    assert retail_row["price_usd"] == pytest.approx(0.087)


def test_a_retail_row_reaches_the_store_without_the_infracost_api(retail_api,
                                                                   monkeypatch):
    """The nested fallback writes into the same cache, under its own source."""
    def no_call(*args, **kwargs):
        raise AssertionError("the Infracost API must not be read for a retail meter")

    monkeypatch.setattr(ic.requests, "post", no_call)
    query = ic.parse_descriptor("Bandwidth-Internet-Out-GB", "eastus", "azure")
    upserted = []
    cache = MagicMock()
    cache.upsert.side_effect = upserted.append
    client = ic.InfracostClient(api_key="k", org_id="o")
    stored = client._store_rows(cache, query, client._fetch_prices(query))
    metered = [r for r in upserted if r.price_usd]
    assert stored == len(upserted)
    assert [(r.vendor, r.service, r.usage_metric, r.unit) for r in metered] == [
        ("azure", "Bandwidth", "Bandwidth-Internet-Out-GB", "GB")]
    assert metered[0].price_usd == pytest.approx(0.087)
    # The retail row replaces Infracost's for this metric, as an Infracost row would.
    cache.replacing.assert_called_once_with(
        "azure", "Bandwidth", "eastus", "Bandwidth-Internet-Out-GB",
        ("infracost", azure_retail.SOURCE))
    assert retail_api, "the retail API is what supplied the row"