"""Tests for external API resource model."""

import pytest
from infra_cost_model.resources.external import (
    ExternalNode,
    ExternalServiceRegistry,
    external_cost,
    stripe_cost,
    twilio_sms_cost,
    sendgrid_cost,
    STRIPE_STANDARD,
)


class TestExternalServiceRegistry:
    """Tests for the data-driven external service registry."""

    def teardown_method(self):
        """Reset registry after each test to avoid cross-test pollution."""
        ExternalServiceRegistry.reset()
        ExternalServiceRegistry.register_many(["external", "stripe", "twilio", "sendgrid"])

    def test_builtin_prefixes_registered(self):
        """Built-in prefixes are registered at module load."""
        assert "external." in ExternalServiceRegistry.known_prefixes()
        assert "stripe." in ExternalServiceRegistry.known_prefixes()
        assert "twilio." in ExternalServiceRegistry.known_prefixes()
        assert "sendgrid." in ExternalServiceRegistry.known_prefixes()

    def test_is_external_matches_registered_prefixes(self):
        """is_external() returns True for addresses matching registered prefixes."""
        assert ExternalServiceRegistry.is_external("external.stripe")
        assert ExternalServiceRegistry.is_external("stripe.payment")
        assert ExternalServiceRegistry.is_external("twilio.sms")
        assert ExternalServiceRegistry.is_external("sendgrid.email")

    def test_is_external_rejects_unknown(self):
        """is_external() returns False for unrecognized addresses."""
        assert not ExternalServiceRegistry.is_external("aws_lambda_function.test")
        assert not ExternalServiceRegistry.is_external("postgres.database")

    def test_register_new_vendor(self):
        """Registering a new vendor makes its addresses recognized."""
        assert not ExternalServiceRegistry.is_external("auth0.login")
        ExternalServiceRegistry.register("auth0")
        assert ExternalServiceRegistry.is_external("auth0.login")
        assert ExternalServiceRegistry.is_external("auth0.user_management")

    def test_register_adds_trailing_dot(self):
        """register() adds trailing dot if missing."""
        ExternalServiceRegistry.register("openai")
        assert "openai." in ExternalServiceRegistry.known_prefixes()

    def test_register_preserves_trailing_dot(self):
        """register() preserves existing trailing dot."""
        ExternalServiceRegistry.register("datadog.")
        assert "datadog." in ExternalServiceRegistry.known_prefixes()

    def test_register_many(self):
        """register_many() registers multiple prefixes at once."""
        ExternalServiceRegistry.register_many(["auth0", "openai", "datadog"])
        assert ExternalServiceRegistry.is_external("auth0.login")
        assert ExternalServiceRegistry.is_external("openai.chat")
        assert ExternalServiceRegistry.is_external("datadog.metrics")

    def test_external_node_respects_registry(self):
        """ExternalNode.from_address() uses the registry, not hardcoded checks."""
        # Built-in services work
        assert ExternalNode.from_address("external.stripe") is not None

        # Unknown service rejected
        ExternalServiceRegistry.reset()
        assert ExternalNode.from_address("external.stripe") is None

        # After registering, it works
        ExternalServiceRegistry.register("external")
        assert ExternalNode.from_address("external.stripe") is not None

        # New vendor works after registration
        ExternalServiceRegistry.register("auth0")
        assert ExternalNode.from_address("auth0.login") is not None


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