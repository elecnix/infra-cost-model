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