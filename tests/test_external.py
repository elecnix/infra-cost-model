"""Tests for external API resource model."""

import pytest
from infra_cost_model.resources.external import (
    ExternalNode,
    external_cost,
    stripe_cost,
    twilio_sms_cost,
    sendgrid_cost,
    STRIPE_STANDARD,
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