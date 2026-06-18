"""Tests for external API resource model."""

import pytest
from infra_cost_model.resources.external import (
    ExternalNode,
    external_cost,
    stripe_cost,
    twilio_sms_cost,
    sendgrid_cost,
)


def test_external_from_address():
    """Test parsing external service addresses."""
    assert ExternalNode.from_address("external.stripe") is not None
    assert ExternalNode.from_address("stripe.payment") is not None
    assert ExternalNode.from_address("twilio.sms") is not None
    assert ExternalNode.from_address("sendgrid.email") is not None
    assert ExternalNode.from_address("aws_lambda_function.test") is None


def test_external_node_type():
    """Test external node type is 'external'."""
    external = ExternalNode.from_address("external.stripe")
    assert external.node_type == "external"


def test_external_valid_metrics():
    """Test external node valid metrics."""
    external = ExternalNode.from_address("external.stripe")
    assert "apiCalls" in external.valid_metrics
    assert "transactionVolume" in external.valid_metrics


def test_external_no_infrastructure_extraction():
    """Test that external nodes cannot be extracted from IaC."""
    with pytest.raises(NotImplementedError, match="no infrastructure resource"):
        ExternalNode.extract_tf({})
    
    with pytest.raises(NotImplementedError, match="no infrastructure resource"):
        ExternalNode.extract_pulumi({})
    
    with pytest.raises(NotImplementedError, match="no infrastructure resource"):
        ExternalNode.extract_cdk({})


def test_stripe_standard_cost():
    """Test Stripe standard pricing."""
    # 10,000 transactions, $500,000 volume
    cost = stripe_cost(10_000, 500_000)
    
    expected = 500_000 * 0.029 + 10_000 * 0.30
    assert cost == pytest.approx(expected)


def test_stripe_international_cost():
    """Test Stripe international card pricing."""
    cost = stripe_cost(10_000, 500_000, international=True)
    
    # Standard + 1% currency conversion
    expected = 500_000 * 0.039 + 10_000 * 0.30 + 500_000 * 0.01
    assert cost == pytest.approx(expected)


def test_twilio_sms_cost():
    """Test Twilio SMS pricing."""
    cost = twilio_sms_cost(100_000)
    assert cost == pytest.approx(750.0)  # 100K * $0.0075


def test_sendgrid_cost():
    """Test SendGrid pricing."""
    cost = sendgrid_cost(50_000)
    assert cost == pytest.approx(5.0)  # 50K * $0.0001


def test_external_cost_with_percentage():
    """Test external cost with percentage model."""
    cost = external_cost(
        transactions=5_000,
        volume=250_000,
        percentage_rate=0.029,
        fixed_per_transaction=0.30,
    )
    
    expected = 250_000 * 0.029 + 5_000 * 0.30
    assert cost == pytest.approx(expected)


def test_external_cost_with_per_call():
    """Test external cost with per-call model."""
    cost = external_cost(
        transactions=10_000,
        volume=0,
        per_call=0.0075,
    )
    
    assert cost == pytest.approx(75.0)


class TestExternalPricingWithCatalog:
    """Tests for external pricing functions using a pricing catalog (Principle 13)."""

    @staticmethod
    def _make_catalog_with_seed_data():
        from infra_cost_model.pricing.cache import PricingCache, Price
        from infra_cost_model.pricing.catalog import PricingCatalog
        from pathlib import Path
        import tempfile
        tmpdir = tempfile.TemporaryDirectory()
        cache = PricingCache(db_path=Path(tmpdir.name) / "test.db")
        # Standard Stripe
        cache.upsert(Price(
            vendor="external", service="ExternalAPI", region="global",
            product_family="External", attributes={},
            usage_metric="external_percentage", unit="USD",
            price_usd=0.029,
            start_usage_amount=None, end_usage_amount=None,
            source="test", effective_date="2024-01-01", fetched_at="2024-01-01T00:00:00"
        ))
        cache.upsert(Price(
            vendor="external", service="ExternalAPI", region="global",
            product_family="External", attributes={},
            usage_metric="external_fixed_per_tx", unit="USD",
            price_usd=0.30,
            start_usage_amount=None, end_usage_amount=None,
            source="test", effective_date="2024-01-01", fetched_at="2024-01-01T00:00:00"
        ))
        # International Stripe
        cache.upsert(Price(
            vendor="external", service="ExternalAPI", region="global",
            product_family="External", attributes={},
            usage_metric="stripe_international_percentage", unit="USD",
            price_usd=0.039,
            start_usage_amount=None, end_usage_amount=None,
            source="test", effective_date="2024-01-01", fetched_at="2024-01-01T00:00:00"
        ))
        cache.upsert(Price(
            vendor="external", service="ExternalAPI", region="global",
            product_family="External", attributes={},
            usage_metric="stripe_international_fixed_per_tx", unit="USD",
            price_usd=0.30,
            start_usage_amount=None, end_usage_amount=None,
            source="test", effective_date="2024-01-01", fetched_at="2024-01-01T00:00:00"
        ))
        # Twilio
        cache.upsert(Price(
            vendor="external", service="ExternalAPI", region="global",
            product_family="External", attributes={},
            usage_metric="twilio_sms", unit="messages",
            price_usd=0.0075,
            start_usage_amount=None, end_usage_amount=None,
            source="test", effective_date="2024-01-01", fetched_at="2024-01-01T00:00:00"
        ))
        # SendGrid
        cache.upsert(Price(
            vendor="external", service="ExternalAPI", region="global",
            product_family="External", attributes={},
            usage_metric="sendgrid_email", unit="emails",
            price_usd=0.0001,
            start_usage_amount=None, end_usage_amount=None,
            source="test", effective_date="2024-01-01", fetched_at="2024-01-01T00:00:00"
        ))
        catalog = PricingCatalog(db_path=Path(tmpdir.name) / "test.db")
        return catalog, tmpdir

    def test_stripe_standard_with_catalog(self):
        """Stripe standard pricing via catalog matches hardcoded fallback."""
        catalog, tmpdir = self._make_catalog_with_seed_data()
        try:
            cost = stripe_cost(10_000, 500_000, catalog=catalog)
            expected = 500_000 * 0.029 + 10_000 * 0.30
            assert cost == pytest.approx(expected)
        finally:
            tmpdir.cleanup()

    def test_stripe_international_with_catalog(self):
        """Stripe international pricing via catalog includes 1% currency fee."""
        catalog, tmpdir = self._make_catalog_with_seed_data()
        try:
            cost = stripe_cost(10_000, 500_000, international=True, catalog=catalog)
            expected = 500_000 * 0.039 + 10_000 * 0.30 + 500_000 * 0.01
            assert cost == pytest.approx(expected)
        finally:
            tmpdir.cleanup()

    def test_twilio_sms_with_catalog(self):
        """Twilio SMS pricing via catalog matches fallback."""
        catalog, tmpdir = self._make_catalog_with_seed_data()
        try:
            cost = twilio_sms_cost(100_000, catalog=catalog)
            assert cost == pytest.approx(750.0)
        finally:
            tmpdir.cleanup()

    def test_sendgrid_with_catalog(self):
        """SendGrid pricing via catalog matches fallback."""
        catalog, tmpdir = self._make_catalog_with_seed_data()
        try:
            cost = sendgrid_cost(50_000, catalog=catalog)
            assert cost == pytest.approx(5.0)
        finally:
            tmpdir.cleanup()

    def test_stripe_no_catalog_fallback(self):
        """Stripe cost with catalog=None uses fallback constants."""
        cost = stripe_cost(10_000, 500_000, catalog=None)
        expected = 500_000 * 0.029 + 10_000 * 0.30
        assert cost == pytest.approx(expected)

    def test_twilio_no_catalog_fallback(self):
        """Twilio cost with catalog=None uses fallback constants."""
        cost = twilio_sms_cost(100_000, catalog=None)
        assert cost == pytest.approx(750.0)