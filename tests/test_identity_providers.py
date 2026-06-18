"""Tests for identity provider pricing models."""

import pytest
from infra_cost_model.pricing.identity_providers import (
    IdentityProviderPricing,
    MAUTier,
    SSOTier,
    WORKOS_PRICING,
    AUTH0_PRICING,
    CLERK_PRICING,
    FRONTEGG_PRICING,
    KINDE_PRICING,
    AWS_COGNITO_PRICING,
    ENTRA_PRICING,
    GCIP_PRICING,
    IDENTITY_PROVIDER_PRICING,
    get_identity_provider,
    list_identity_providers,
    compute_mau_cost,
    compute_sso_cost,
    compute_total_cost,
)


class TestIdentityProviderRegistry:
    """Tests for the identity provider registry."""

    def test_all_eight_providers_registered(self):
        """All 8 identity providers are registered."""
        providers = list_identity_providers()
        assert len(providers) == 8
        assert "workos" in providers
        assert "auth0" in providers
        assert "clerk" in providers
        assert "frontegg" in providers
        assert "kinde" in providers
        assert "aws_cognito" in providers
        assert "entra_external_id" in providers
        assert "gcp_identity_platform" in providers

    def test_get_identity_provider(self):
        """get_identity_provider returns correct Pricing objects."""
        p = get_identity_provider("clerk")
        assert p is not None
        assert p.name == "Clerk"
        assert p.vendor == "clerk"

    def test_get_nonexistent_provider(self):
        """get_identity_provider returns None for unknown provider."""
        assert get_identity_provider("nonexistent") is None


class TestMAUCostComputation:
    """Tests for MAU-based cost computation."""

    def test_within_free_tier(self):
        """MAU within free tier costs $0."""
        cost = compute_mau_cost(CLERK_PRICING, 5_000)
        assert cost == 0.0

    def test_at_free_tier_boundary(self):
        """MAU exactly at free tier costs $0."""
        cost = compute_mau_cost(CLERK_PRICING, 10_000)
        assert cost == 0.0

    def test_clerk_step_function_pricing(self):
        """Clerk step-function: 15K MAU = just the 10K-25K tier."""
        # 15K MAU: 10K free, 5K billed at $0.02 = $100
        cost = compute_mau_cost(CLERK_PRICING, 15_000)
        assert cost == pytest.approx(100.0)  # 5K * $0.02

    def test_clerk_crossing_multiple_tiers(self):
        """Clerk: 60K MAU crosses multiple step-function tiers."""
        # 60K MAU: 10K free, then:
        #   10K-25K: 15K * $0.02 = $300
        #   25K-50K: 25K * $0.015 = $375
        #   50K-60K: 10K * $0.01 = $100
        #   Total: $775
        cost = compute_mau_cost(CLERK_PRICING, 60_000)
        assert cost == pytest.approx(775.0)

    def test_clerk_large_scale(self):
        """Clerk: 200K MAU covers all tiers."""
        # 200K MAU: 10K free, then:
        #   10K-25K: 15K * $0.02 = $300
        #   25K-50K: 25K * $0.015 = $375
        #   50K-100K: 50K * $0.01 = $500
        #   100K-200K: 100K * $0.0075 = $750
        #   Total: $1,925
        cost = compute_mau_cost(CLERK_PRICING, 200_000)
        assert cost == pytest.approx(1925.0)

    def test_aws_cognito_graduated_tiers(self):
        """AWS Cognito: 200K MAU using graduated tiering."""
        # 200K MAU: first 50K free, then:
        #   50K-100K: 50K * $0.0055 = $275
        #   100K-200K: 100K * $0.0046 = $460
        #   Total: $735
        cost = compute_mau_cost(AWS_COGNITO_PRICING, 200_000)
        assert cost == pytest.approx(735.0)

    def test_aws_cognito_massive_scale(self):
        """AWS Cognito: 15M MAU covers all tiers."""
        # 15M MAU: first 50K free, then:
        #   50K-100K: 50K * $0.0055 = $275
        #   100K-1M: 900K * $0.0046 = $4,140
        #   1M-10M: 9M * $0.00325 = $29,250
        #   10M-15M: 5M * $0.0025 = $12,500
        #   Total: $46,165
        cost = compute_mau_cost(AWS_COGNITO_PRICING, 15_000_000)
        assert cost == pytest.approx(46165.0)

    def test_entra_graduated_tiers(self):
        """Entra External ID: 500K MAU."""
        # First 50K free, then:
        #   50K-300K: 250K * $0.03 = $7,500
        #   300K-500K: 200K * $0.02 = $4,000
        #   Total: $11,500
        cost = compute_mau_cost(ENTRA_PRICING, 500_000)
        assert cost == pytest.approx(11500.0)

    def test_gcip_simple_two_tier(self):
        """GCIP: 500K MAU with simple two-tier structure."""
        # First 50K free, then 450K * $0.0055 = $2,475
        cost = compute_mau_cost(GCIP_PRICING, 500_000)
        assert cost == pytest.approx(2475.0)

    def test_no_tiers_returns_zero(self):
        """Provider with no MAU tiers returns 0."""
        pricing = IdentityProviderPricing(
            name="Test", vendor="test",
            mau_free_tier=1000, mau_tiers=[]
        )
        assert compute_mau_cost(pricing, 5_000) == 0.0


class TestSSOCostComputation:
    """Tests for SSO connection cost computation."""

    def test_within_included_connections(self):
        """SSO connections within included limit cost $0."""
        cost = compute_sso_cost(KINDE_PRICING, 3)
        assert cost == 0.0

    def test_exactly_at_included_limit(self):
        """SSO connections at included limit cost $0."""
        cost = compute_sso_cost(KINDE_PRICING, 5)
        assert cost == 0.0

    def test_exceeds_included(self):
        """SSO connections exceeding included limit."""
        # Kinde: 5 included, $25/additional
        # 7 connections: 2 billable * $25 = $50
        cost = compute_sso_cost(KINDE_PRICING, 7)
        assert cost == pytest.approx(50.0)

    def test_workos_flat_sso(self):
        """WorkOS flat SSO pricing."""
        # $225/connection, no included connections
        cost = compute_sso_cost(WORKOS_PRICING, 3)
        assert cost == pytest.approx(675.0)

    def test_clerk_sso(self):
        """Clerk SSO pricing."""
        # $200/connection, no included on Pro
        cost = compute_sso_cost(CLERK_PRICING, 2)
        assert cost == pytest.approx(400.0)


class TestTotalCostComputation:
    """Tests for multi-axis total cost computation."""

    def test_mau_only(self):
        """Total cost with only MAU."""
        costs = compute_total_cost(CLERK_PRICING, mau=25_000)
        # 25K MAU: 10K free, 15K * $0.02 = $300
        assert costs["mau"] == pytest.approx(300.0)
        assert costs["total"] == pytest.approx(300.0)

    def test_mau_plus_sso(self):
        """Total cost with MAU and SSO connections."""
        costs = compute_total_cost(CLERK_PRICING, mau=25_000, sso_connections=2)
        # MAU: $300, SSO: 2 * $200 = $400
        assert costs["mau"] == pytest.approx(300.0)
        assert costs["sso"] == pytest.approx(400.0)
        assert costs["total"] == pytest.approx(700.0)

    def test_all_axes(self):
        """Total cost with all pricing axes."""
        costs = compute_total_cost(
            CLERK_PRICING,
            mau=50_000,
            sso_connections=3,
            m2m_tokens=3_000_000,
        )
        # MAU: 10K free, 15K*$0.02 + 25K*$0.015 = 300+375 = $675
        # SSO: 3 * $200 = $600
        # M2M: 1M free, 2M billable = 2000K * $0.001 = $2
        assert costs["mau"] == pytest.approx(675.0)
        assert costs["sso"] == pytest.approx(600.0)
        assert costs["m2m"] == pytest.approx(2.0)
        assert costs["total"] == pytest.approx(1277.0)

    def test_scim_cost(self):
        """SCIM connection cost."""
        costs = compute_total_cost(WORKOS_PRICING, mau=1_000_000, scim_connections=3)
        # SCIM: 3 * $125 = $375
        assert costs["scim"] == pytest.approx(375.0)

    def test_mfa_cost(self):
        """MFA verification cost."""
        costs = compute_total_cost(GCIP_PRICING, mau=100_000, mfa_verifications=10_000)
        # MFA: 10K * $0.01 = $100
        assert costs["mfa"] == pytest.approx(100.0)


class TestProviderPricingConsistency:
    """Sanity checks that all provider pricing data is internally consistent."""

    def test_all_providers_have_names_and_vendors(self):
        """All providers have non-empty name and vendor."""
        for key, p in IDENTITY_PROVIDER_PRICING.items():
            assert p.name, f"{key} has no name"
            assert p.vendor, f"{key} has no vendor"

    def test_all_providers_have_pricing_source(self):
        """All providers cite their pricing source."""
        for key, p in IDENTITY_PROVIDER_PRICING.items():
            assert p.pricing_source, f"{key} has no pricing_source"

    def test_mau_tiers_are_non_overlapping_monotonic(self):
        """MAU tiers don't overlap and are monotonically increasing."""
        for key, p in IDENTITY_PROVIDER_PRICING.items():
            prev_end = 0
            for tier in p.mau_tiers:
                assert tier.start_mau >= prev_end, (
                    f"{key}: tier start {tier.start_mau} < prev_end {prev_end}"
                )
                if tier.end_mau is not None:
                    assert tier.end_mau > tier.start_mau, (
                        f"{key}: tier end {tier.end_mau} <= start {tier.start_mau}"
                    )
                    prev_end = tier.end_mau

    def test_free_tier_consistent_with_first_tier(self):
        """If first tier has $0 price, it should cover the free tier range."""
        for key, p in IDENTITY_PROVIDER_PRICING.items():
            if p.mau_tiers and p.mau_tiers[0].price_per_mau == 0.0:
                first_tier = p.mau_tiers[0]
                # The $0 tier should extend at least to the declared free tier
                if first_tier.end_mau is not None:
                    assert first_tier.end_mau >= p.mau_free_tier, (
                        f"{key}: $0 tier ends at {first_tier.end_mau} "
                        f"but free_tier is {p.mau_free_tier}"
                    )

    def test_caps_are_non_negative(self):
        """Caps are either None or non-negative."""
        for key, p in IDENTITY_PROVIDER_PRICING.items():
            if p.mau_cap is not None:
                assert p.mau_cap >= 0, f"{key}: negative mau_cap"
            if p.sso_cap is not None:
                assert p.sso_cap >= 0, f"{key}: negative sso_cap"
