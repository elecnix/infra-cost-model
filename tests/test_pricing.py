"""Tests for pricing catalog and cache."""

import pytest
from pathlib import Path
import tempfile
import sqlite3

from infra_cost_model.pricing.cache import PricingCache, Price, TieredPrice
from infra_cost_model.pricing.catalog import PricingCatalog
from infra_cost_model.pricing.sources.infracost import _sync_fallback


def test_cache_upsert_and_query():
    """Test basic cache operations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = PricingCache(db_path=Path(tmpdir) / "test.db")

        price = Price(
            vendor="aws",
            service="AWSLambda",
            region="us-east-1",
            product_family="Serverless",
            attributes={},
            usage_metric="Lambda-Request",
            unit="requests",
            price_usd=0.20e-6,
            source="test",
            effective_date="2024-01-01",
            fetched_at="2024-01-01T00:00:00"
        )

        cache.upsert(price)

        result = cache.query("aws", "AWSLambda", "us-east-1", "Lambda-Request")
        assert result is not None
        assert result.price_usd == 0.20e-6


def test_tiered_pricing():
    """Test tiered pricing calculation."""
    tiers = [
        Price(
            vendor="aws", service="AWSLambda", region="us-east-1",
            product_family="Serverless", attributes={},
            usage_metric="Lambda-GB-Second", unit="GB-s",
            price_usd=0.0000166667,
            start_usage_amount=0,
            end_usage_amount=6_000_000_000,
            source="test",
            effective_date="2024-01-01",
            fetched_at="2024-01-01T00:00:00"
        ),
        Price(
            vendor="aws", service="AWSLambda", region="us-east-1",
            product_family="Serverless", attributes={},
            usage_metric="Lambda-GB-Second", unit="GB-s",
            price_usd=0.000015,
            start_usage_amount=6_000_000_000,
            end_usage_amount=15_000_000_000,
            source="test",
            effective_date="2024-01-01",
            fetched_at="2024-01-01T00:00:00"
        ),
    ]

    tp = TieredPrice(tiers=tiers)

    # 10B GB-seconds should span two tiers
    cost = tp.total_cost(10_000_000_000)
    expected = 6_000_000_000 * 0.0000166667 + 4_000_000_000 * 0.000015
    assert abs(cost - expected) < 1.0


def test_catalog_query():
    """Test PricingCatalog query interface."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = PricingCache(db_path=Path(tmpdir) / "test.db")

        price = Price(
            vendor="aws", service="AWSLambda", region="us-east-1",
            product_family="Serverless", attributes={},
            usage_metric="Lambda-Request", unit="requests",
            price_usd=0.20e-6,
            source="test",
            effective_date="2024-01-01",
            fetched_at="2024-01-01T00:00:00"
        )
        cache.upsert(price)

        catalog = PricingCatalog(Path(tmpdir) / "test.db")
        result = catalog.query("aws", "AWSLambda", "us-east-1", "Lambda-Request", 1_000_000)

        assert result is not None
        # $0.20 per million * 1M = $0.20
        assert result.total_cost == pytest.approx(0.20, rel=0.01)


def test_fallback_sync():
    """Test fallback pricing sync (no auth required)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = PricingCache(db_path=Path(tmpdir) / "test.db")
        count, source = _sync_fallback("aws", ["AWSLambda"], cache)

        assert count > 0
        assert source == "aws-pricelist"
        assert not cache.is_stale("aws", "AWSLambda")


def test_fallback_sync_uses_model_metric_names():
    """Test fallback prices can be queried by catalog metric names."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = PricingCache(db_path=Path(tmpdir) / "test.db")
        _sync_fallback("aws", ["AWSLambda"], cache)

        requests_price = cache.query("aws", "AWSLambda", "us-east-1", "Lambda-Request")
        duration_price = cache.query("aws", "AWSLambda", "us-east-1", "Lambda-GB-Second")

        assert requests_price is not None
        assert duration_price is not None


def test_free_tier_handling():
    """Test that free tier prices are represented correctly."""
    free_tier_price = Price(
        vendor="aws", service="AWSLambda", region="us-east-1",
        product_family="Serverless", attributes={},
        usage_metric="Lambda-Request", unit="requests",
        price_usd=0.0,  # Free tier has zero price
        start_usage_amount=0,
        end_usage_amount=1_000_000,
        source="aws-fallback",
        effective_date="2024-01-01",
        fetched_at="2024-01-01T00:00:00"
    )

    assert free_tier_price.price_usd == 0.0


def test_tiered_result_cost_calculation():
    """Test cost result with free tier and paid tier."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = PricingCache(db_path=Path(tmpdir) / "test.db")

        now = "2024-01-01T00:00:00"

        # Free tier: 1M requests at $0
        free = Price(
            vendor="aws", service="AWSLambda", region="us-east-1",
            product_family="Serverless", attributes={},
            usage_metric="Lambda-Request", unit="requests",
            price_usd=0.0,
            start_usage_amount=0,
            end_usage_amount=1_000_000,
            source="aws-fallback",
            effective_date=now,
            fetched_at=now
        )
        # Paid tier: requests above 1M at $0.20 per million
        paid = Price(
            vendor="aws", service="AWSLambda", region="us-east-1",
            product_family="Serverless", attributes={},
            usage_metric="Lambda-Request", unit="requests",
            price_usd=0.20e-6,
            start_usage_amount=1_000_000,
            end_usage_amount=None,
            source="aws-fallback",
            effective_date=now,
            fetched_at=now
        )

        cache.upsert(free)
        cache.upsert(paid)

        catalog = PricingCatalog(Path(tmpdir) / "test.db")
        result = catalog.query("aws", "AWSLambda", "us-east-1", "Lambda-Request", 2_000_000)

        assert result is not None
        # 2M requests - 1M free = 1M paid at $0.20/M = $0.20
        assert result.total_cost == pytest.approx(0.20, rel=0.01)


def test_seed_file_loading():
    """Test loading prices from seed file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = PricingCache(db_path=Path(tmpdir) / "test.db")
        count, source = _sync_fallback("aws", ["AWSLambda"], cache)

        assert count > 0
        assert source == "aws-pricelist"
        
        # Verify prices were loaded
        result = cache.query("aws", "AWSLambda", "us-east-1", "Lambda-Request")
        assert result is not None

# ── Seed data integrity and duplicate-row handling (catalog robustness) ───────

def test_seed_file_is_valid_json():
    """The bundled seed price list must be parseable, non-empty JSON.

    A malformed seed file makes seed_prices() raise and silently leaves the
    catalog empty, so every catalog-backed cost computes as $0.
    """
    import json
    from infra_cost_model.pricing.cache import SEED_PRICES_PATH

    data = json.loads(Path(SEED_PRICES_PATH).read_text())
    assert isinstance(data, list)
    assert len(data) > 0
    for entry in data:
        assert "vendor" in entry and "service" in entry
        assert "usage_metric" in entry and "price_usd" in entry


def _seed_price(**overrides) -> Price:
    base = dict(
        vendor="aws", service="AmazonS3", region="us-east-1",
        product_family=None, attributes={}, usage_metric="S3-Storage",
        unit="GB-Mo", price_usd=0.023, start_usage_amount=0.0,
        end_usage_amount=51200.0, purchase_option=None,
        source="seed", effective_date="2024-01-01",
        fetched_at="2024-01-01T00:00:00",
    )
    base.update(overrides)
    return Price(**base)


def test_query_deduplicates_identical_rows():
    """Identical rows (e.g. re-seeded with NULL purchase_option) collapse to one.

    SQLite treats NULL as distinct in a UNIQUE constraint, so seed rows with
    purchase_option=NULL can be inserted repeatedly. A query must not return a
    single logical price as a spurious multi-tier TieredPrice.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = PricingCache(db_path=Path(tmpdir) / "test.db")
        price = _seed_price(end_usage_amount=None)  # single flat tier
        for _ in range(5):
            cache.upsert(price)

        result = cache.query("aws", "AmazonS3", "us-east-1", "S3-Storage")
        assert not isinstance(result, TieredPrice)
        assert result.price_usd == pytest.approx(0.023)


def test_tiered_cost_unaffected_by_duplicate_rows():
    """Tiered cost is correct even when each tier row is duplicated."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = PricingCache(db_path=Path(tmpdir) / "test.db")
        tier1 = _seed_price(start_usage_amount=0.0, end_usage_amount=51200.0, price_usd=0.023)
        tier2 = _seed_price(start_usage_amount=51200.0, end_usage_amount=512000.0, price_usd=0.022)
        for _ in range(3):
            cache.upsert(tier1)
            cache.upsert(tier2)

        catalog = PricingCatalog(db_path=Path(tmpdir) / "test.db")
        # 100 GB-Mo within the first tier: 100 × $0.023, NOT multiplied by dups.
        result = catalog.query("aws", "AmazonS3", "us-east-1", "S3-Storage", 100)
        assert result.total_cost == pytest.approx(2.30)


def test_seed_prices_is_idempotent():
    """Loading seed prices twice must not accumulate duplicate rows."""
    from infra_cost_model.pricing.cache import seed_prices

    with tempfile.TemporaryDirectory() as tmpdir:
        cache = PricingCache(db_path=Path(tmpdir) / "test.db")
        seed_prices(cache)
        conn = sqlite3.connect(cache.db_path)
        count_once = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
        conn.close()

        seed_prices(cache)
        conn = sqlite3.connect(cache.db_path)
        count_twice = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
        conn.close()

        assert count_once > 0
        assert count_twice == count_once
