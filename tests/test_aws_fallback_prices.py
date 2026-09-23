"""Tests for the services argument of aws_fallback_prices (issue #278)."""

import pytest

from infra_cost_model.pricing.cache import PricingCache
from infra_cost_model.pricing.sources import aws_pricing
from infra_cost_model.pricing.sources.aws_pricing import SERVICE_CODES, aws_fallback_prices


@pytest.fixture
def api_only(monkeypatch, tmp_path):
    """Hide the seed file and replace the AWS Price List call with a stub.

    Returns the list of AWS service codes the stub was asked for.
    """
    monkeypatch.setattr(aws_pricing, "SEED_PRICES_PATH", tmp_path / "missing.json")
    requested: list[str] = []

    def fake_fetch(service_code):
        requested.append(service_code)
        return [{
            "sku": f"{service_code}-sku",
            "service": service_code,
            "attributes": {"regionCode": "us-east-1", "operation": "Invoke request"},
            "unit": "Requests",
            "price_usd": 0.2e-6,
        }]

    monkeypatch.setattr(aws_pricing, "fetch_aws_price_list", fake_fetch)
    return requested


def test_none_fetches_every_known_service(api_only, tmp_path):
    cache = PricingCache(db_path=tmp_path / "prices.db")
    count = aws_fallback_prices(None, cache)
    assert sorted(api_only) == sorted(SERVICE_CODES.values())
    assert count > 0


def test_empty_list_raises_value_error(api_only, tmp_path):
    cache = PricingCache(db_path=tmp_path / "prices.db")
    with pytest.raises(ValueError, match="services"):
        aws_fallback_prices([], cache)
    assert api_only == []


def test_sync_fallback_passes_none_through(api_only, tmp_path):
    from infra_cost_model.pricing.sources.infracost import _sync_fallback

    cache = PricingCache(db_path=tmp_path / "prices.db")
    count, source = _sync_fallback("aws", None, cache)
    assert source == "aws-pricelist" and count > 0
    assert sorted(api_only) == sorted(SERVICE_CODES.values())
