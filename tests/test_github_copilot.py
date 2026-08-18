"""Tests for GitHub Copilot pricing (usage-based billing, June 2026).

Covers the plan registry, per-model token rates, credit estimation, and the
subscription + overage cost computation. The authoritative pricing data lives
in infra_cost_model/pricing/github_copilot.py; sources are cited in
docs/github-copilot-pricing.md.
"""

import pytest

from infra_cost_model.pricing.github_copilot import (
    COPILOT_PLANS,
    COPILOT_MODEL_RATES,
    CREDIT_VALUE_USD,
    compute_copilot_cost,
    copilot_plan_cost,
    estimate_credits,
    get_copilot_plan,
    list_copilot_plans,
)


class TestCopilotPlanRegistry:
    """The plan registry mirrors GitHub's published plans."""

    def test_all_six_plans_registered(self):
        assert set(list_copilot_plans()) == {
            "free", "pro", "pro_plus", "max", "business", "enterprise",
        }

    def test_get_plan(self):
        plan = get_copilot_plan("business")
        assert plan is not None
        assert plan.name == "Copilot Business"
        assert plan.price_usd == 19.0
        assert plan.included_credits == 1_900
        assert plan.per_user is True

    def test_get_nonexistent_plan(self):
        assert get_copilot_plan("nonexistent") is None

    def test_individual_plan_allowances(self):
        """Pro: $10/mo with 1,500 credits (1,000 base + 500 flex)."""
        pro = COPILOT_PLANS["pro"]
        assert pro.price_usd == 10.0
        assert pro.base_credits == 1_000
        assert pro.flex_credits == 500
        assert pro.included_credits == 1_500
        assert pro.per_user is False

    def test_org_plan_allowances(self):
        """Business 1,900 and Enterprise 3,900 credits per user per month."""
        assert COPILOT_PLANS["business"].included_credits == 1_900
        assert COPILOT_PLANS["enterprise"].included_credits == 3_900
        assert COPILOT_PLANS["enterprise"].price_usd == 39.0

    def test_credit_value_constant(self):
        assert CREDIT_VALUE_USD == 0.01


class TestModelRates:
    """Per-1M-token rates for the representative model subset."""

    def test_representative_models_present(self):
        for key in ("gpt-5.4", "gpt-5.5", "claude-sonnet-4.5", "claude-opus-4.5"):
            assert key in COPILOT_MODEL_RATES

    def test_output_dearer_than_input(self):
        """Output tokens cost 5-6x input on every model."""
        for rate in COPILOT_MODEL_RATES.values():
            assert rate.output > rate.input

    def test_cached_input_discount(self):
        """Cached input is ~10% of fresh input."""
        for rate in COPILOT_MODEL_RATES.values():
            assert rate.cached_input < rate.input

    def test_anthropic_cache_write(self):
        """Anthropic models carry a cache-write cost."""
        sonnet = COPILOT_MODEL_RATES["claude-sonnet-4.5"]
        assert sonnet.cache_write == 3.75

    def test_openai_legacy_no_cache_write(self):
        """Pre-5.6 OpenAI models have no cache-write cost."""
        assert COPILOT_MODEL_RATES["gpt-5.4"].cache_write == 0.0


class TestEstimateCredits:
    """Token consumption → AI credits (1 credit = $0.01)."""

    def test_simple_interaction(self):
        """1M input + 100K output on GPT-5.4: $2.50 + $1.50 = $4.00 = 400 credits."""
        credits = estimate_credits("gpt-5.4", input_tokens=1_000_000, output_tokens=100_000)
        assert credits == pytest.approx(400.0)

    def test_cached_input_cheaper(self):
        """Cached input bills at the discounted rate."""
        fresh = estimate_credits("gpt-5.4", input_tokens=1_000_000, output_tokens=0)
        cached = estimate_credits("gpt-5.4", input_tokens=0, cached_input_tokens=1_000_000, output_tokens=0)
        assert cached == pytest.approx(fresh / 10)

    def test_cache_write_included(self):
        """Claude Sonnet 4.5: 1M cache-write tokens = $3.75 = 375 credits."""
        credits = estimate_credits(
            "claude-sonnet-4.5", input_tokens=0, output_tokens=0, cache_write_tokens=1_000_000
        )
        assert credits == pytest.approx(375.0)

    def test_unknown_model_raises(self):
        with pytest.raises(KeyError):
            estimate_credits("nonexistent-model", input_tokens=1, output_tokens=1)


class TestCopilotPlanCost:
    """Subscription + overage above the included allowance."""

    def test_within_allowance(self):
        """Pro, 1,000 credits used (of 1,500): just the $10 subscription."""
        assert copilot_plan_cost(COPILOT_PLANS["pro"], 1_000) == pytest.approx(10.0)

    def test_exactly_at_allowance(self):
        assert copilot_plan_cost(COPILOT_PLANS["pro"], 1_500) == pytest.approx(10.0)

    def test_overage_billed_per_credit(self):
        """Pro, 2,000 credits: $10 + 500 × $0.01 = $15."""
        assert copilot_plan_cost(COPILOT_PLANS["pro"], 2_000) == pytest.approx(15.0)

    def test_free_plan(self):
        assert copilot_plan_cost(COPILOT_PLANS["free"], 0) == pytest.approx(0.0)


class TestComputeCopilotCost:
    """Full breakdown, including per-seat org plans."""

    def test_individual_plan(self):
        result = compute_copilot_cost(COPILOT_PLANS["pro"], credits_used=2_000)
        assert result["subscription"] == 10.0
        assert result["included_credits"] == 1_500
        assert result["overage_credits"] == 500.0
        assert result["overage_cost"] == 5.0
        assert result["total"] == 15.0

    def test_business_seats_scale_subscription_and_pool(self):
        """25 seats × $19 = $475; pool = 25 × 1,900 = 47,500 credits."""
        result = compute_copilot_cost(COPILOT_PLANS["business"], credits_used=60_000, seats=25)
        assert result["subscription"] == 475.0
        assert result["included_credits"] == 47_500
        assert result["overage_credits"] == 12_500.0
        assert result["overage_cost"] == 125.0
        assert result["total"] == 600.0

    def test_business_within_pool(self):
        result = compute_copilot_cost(COPILOT_PLANS["business"], credits_used=40_000, seats=25)
        assert result["overage_credits"] == 0.0
        assert result["total"] == 475.0

    def test_enterprise_single_seat(self):
        result = compute_copilot_cost(COPILOT_PLANS["enterprise"], credits_used=3_900)
        assert result["total"] == 39.0
        result = compute_copilot_cost(COPILOT_PLANS["enterprise"], credits_used=4_900)
        assert result["total"] == pytest.approx(49.0)
