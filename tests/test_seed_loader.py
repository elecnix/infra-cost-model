"""One loader reads the seed file for every caller (issue #308)."""

import json
import sqlite3

import pytest

from infra_cost_model.pricing import cache as cache_module
from infra_cost_model.pricing.cache import PricingCache, load_seed_rows, seed_prices
from infra_cost_model.pricing.sources import aws_pricing
from infra_cost_model.pricing.sources.aws_pricing import SERVICE_CODES, aws_fallback_prices

LAMBDA_ROW = {
    "vendor": "aws", "service": "AWSLambda", "region": "us-east-1",
    "usage_metric": "Lambda-Request", "unit": "requests", "price_usd": 0.5,
    "attributes": {"tier": "test"}, "per": "seats", "source": "seed",
}
SQS_ROW = {
    "vendor": "aws", "service": "AmazonSQS", "region": "us-east-1",
    "usage_metric": "SQS-Standard-Request", "unit": "requests", "price_usd": 0.4e-6,
    "source": "seed",
}


@pytest.fixture
def seed_file(monkeypatch, tmp_path):
    """Point the seed path in cache.py, and only there, at a small test file."""
    path = tmp_path / "seed.json"
    path.write_text(json.dumps([LAMBDA_ROW, SQS_ROW]))
    monkeypatch.setattr(cache_module, "SEED_PRICES_PATH", path)
    return path


@pytest.fixture
def missing_seed_file(monkeypatch, tmp_path):
    path = tmp_path / "missing.json"
    monkeypatch.setattr(cache_module, "SEED_PRICES_PATH", path)
    return path


@pytest.fixture
def requested(monkeypatch):
    """Replace the AWS Price List call with a stub that returns no rows."""
    calls: list[str] = []

    def fake_fetch(service_code):
        calls.append(service_code)
        return []

    monkeypatch.setattr(aws_pricing, "fetch_aws_price_list", fake_fetch)
    return calls


def seed_rows(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT service, usage_metric, attributes, per, source FROM prices "
            "WHERE source IN ('seed', 'seed-initial') ORDER BY service").fetchall()
    finally:
        conn.close()


def test_loader_returns_every_row_without_a_list(seed_file):
    rows = load_seed_rows()
    assert [row.service for row in rows] == ["AWSLambda", "AmazonSQS"]


def test_loader_keeps_only_the_listed_services(seed_file):
    rows = load_seed_rows(["AmazonSQS"])
    assert [row.service for row in rows] == ["AmazonSQS"]


def test_loader_keeps_attributes_and_per(seed_file):
    (row,) = load_seed_rows(["AWSLambda"])
    assert row.attributes == {"tier": "test"}
    assert row.per == "seats"
    assert row.source == "seed"


def test_loader_raises_when_the_file_is_missing(missing_seed_file):
    with pytest.raises(RuntimeError, match="not found"):
        load_seed_rows()


def test_explicit_list_reads_the_patched_path_and_keeps_every_field(seed_file, tmp_path):
    cache = PricingCache(db_path=tmp_path / "prices.db")
    count = aws_fallback_prices(["AWSLambda"], cache, seed_only=True)
    assert count == 1
    assert seed_rows(cache.db_path) == [
        ("AWSLambda", "Lambda-Request", '{"tier": "test"}', "seats", "seed"),
    ]


def test_explicit_list_and_seed_prices_write_the_same_source(seed_file, tmp_path):
    first = PricingCache(db_path=tmp_path / "first.db")
    second = PricingCache(db_path=tmp_path / "second.db")
    aws_fallback_prices(["AWSLambda", "AmazonSQS"], first, seed_only=True)
    seed_prices(second)
    assert seed_rows(first.db_path) == seed_rows(second.db_path)


def test_none_path_with_a_missing_seed_file_fetches_live(missing_seed_file, requested, tmp_path):
    """The existence check reads the patched path, so seed_prices never runs."""
    cache = PricingCache(db_path=tmp_path / "prices.db")
    with pytest.raises(RuntimeError, match="not found"):
        aws_fallback_prices(None, cache)
    assert sorted(requested) == sorted(SERVICE_CODES.values())


def test_seed_only_with_a_missing_seed_file_says_so(missing_seed_file, tmp_path):
    cache = PricingCache(db_path=tmp_path / "prices.db")
    for services in (None, ["AWSLambda"]):
        with pytest.raises(RuntimeError, match="not found"):
            aws_fallback_prices(services, cache, seed_only=True)


def test_error_names_the_services_the_seed_file_lacks(seed_file, requested, tmp_path):
    cache = PricingCache(db_path=tmp_path / "prices.db")
    with pytest.warns(UserWarning, match="NotAnAwsService"):
        with pytest.raises(RuntimeError) as excinfo:
            aws_fallback_prices(["NotAnAwsService"], cache)
    message = str(excinfo.value)
    assert "not found" not in message
    assert "no rows for NotAnAwsService" in message
    assert str(seed_file) in message


def test_error_says_the_seed_file_is_missing(missing_seed_file, requested, tmp_path):
    cache = PricingCache(db_path=tmp_path / "prices.db")
    with pytest.raises(RuntimeError, match="not found") as excinfo:
        aws_fallback_prices(["AWSLambda"], cache)
    assert str(missing_seed_file) in str(excinfo.value)


def test_seed_pricing_catalog_with_a_list_keeps_every_field(seed_file, monkeypatch, tmp_path):
    from infra_cost_model.pricing.sources.infracost import seed_pricing_catalog

    monkeypatch.setattr(cache_module, "DB_PATH", tmp_path / "prices.db")
    count, source = seed_pricing_catalog(["AWSLambda"])
    assert (count, source) == (1, "seed-pricelist")
    assert seed_rows(tmp_path / "prices.db")[0][2:] == ('{"tier": "test"}', "seats", "seed")


def test_seed_pricing_catalog_with_a_missing_seed_file_raises(missing_seed_file, monkeypatch, tmp_path):
    from infra_cost_model.pricing.sources.infracost import seed_pricing_catalog

    monkeypatch.setattr(cache_module, "DB_PATH", tmp_path / "prices.db")
    for services in (None, ["AWSLambda"]):
        with pytest.raises(RuntimeError, match="not found"):
            seed_pricing_catalog(services)
