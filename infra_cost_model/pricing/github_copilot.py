"""GitHub Copilot pricing — plans, AI credits, and per-model token rates.

Since June 1, 2026, all GitHub Copilot plans bill usage-based: every
model interaction consumes tokens (input, cached input, cache write,
output), each token is priced per model, and the dollar cost is converted
into GitHub AI Credits at a fixed rate of 1 credit = $0.01 USD.

Each plan includes a monthly credit allowance:

- Individual plans (Pro, Pro+, Max) include base credits matching the
  subscription price plus a variable "flex allotment".
- Business and Enterprise include a per-user allowance pooled at the
  billing-entity level (an org with 100 Business seats shares one pool of
  190,000 credits).
- Code completions and next-edit suggestions are NOT billed in credits and
  remain unlimited on all paid plans.

When usage exceeds the included allowance, overage is billed at the
per-token rates (equivalently $0.01 per credit). This module is the
authoritative pricing data; the ``credit_pool`` SaaS shape in
:mod:`infra_cost_model.saas.pricing_shapes` prices a model node against
this data.

Sources:
- https://docs.github.com/en/copilot/concepts/billing/usage-based-billing-for-individuals
- https://docs.github.com/en/copilot/concepts/billing/usage-based-billing-for-organizations-and-enterprises
- https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing
- https://github.blog/news-insights/company-news/github-copilot-is-moving-to-usage-based-billing/
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

CREDIT_VALUE_USD = 0.01  # 1 AI credit = $0.01 USD


@dataclass(frozen=True)
class CopilotPlan:
    """A GitHub Copilot plan's subscription price and monthly credit allowance.

    Attributes:
        name: Plan display name.
        price_usd: Monthly subscription price (per user for org plans).
        included_credits: Monthly included AI credits (base + flex for
            individual plans; per-user pooled allowance for org plans).
        base_credits: The fixed portion of the allowance (individual plans).
        flex_credits: The variable "flex allotment" (individual plans).
        per_user: Whether the price and allowance are per user (org plans).
        notes: Billing notes, e.g. promotional allowances.
    """

    name: str
    price_usd: float
    included_credits: int
    base_credits: int = 0
    flex_credits: int = 0
    per_user: bool = False
    notes: str = ""


COPILOT_PLANS: dict[str, CopilotPlan] = {
    "free": CopilotPlan(
        name="Copilot Free",
        price_usd=0.0,
        included_credits=0,
        notes="No subscription. Limited to 2,000 code completions and 50 chat "
              "requests per month; small AI-credit allowance via auto model "
              "selection only.",
    ),
    "pro": CopilotPlan(
        name="Copilot Pro",
        price_usd=10.0,
        included_credits=1_500,
        base_credits=1_000,
        flex_credits=500,
        notes="Base credits match the subscription price; flex allotment is a "
              "variable monthly top-up.",
    ),
    "pro_plus": CopilotPlan(
        name="Copilot Pro+",
        price_usd=39.0,
        included_credits=7_000,
        base_credits=3_900,
        flex_credits=3_100,
        notes="",
    ),
    "max": CopilotPlan(
        name="Copilot Max",
        price_usd=100.0,
        included_credits=20_000,
        base_credits=10_000,
        flex_credits=10_000,
        notes="",
    ),
    "business": CopilotPlan(
        name="Copilot Business",
        price_usd=19.0,
        included_credits=1_900,
        per_user=True,
        notes="Per-user allowance pooled at the billing-entity level. Existing "
              "customers receive 3,000 credits/user June 1 – September 1, 2026.",
    ),
    "enterprise": CopilotPlan(
        name="Copilot Enterprise",
        price_usd=39.0,
        included_credits=3_900,
        per_user=True,
        notes="Per-user allowance pooled at the billing-entity level. Existing "
              "customers receive 7,000 credits/user June 1 – September 1, 2026. "
              "Requires GitHub Enterprise Cloud.",
    ),
}


@dataclass(frozen=True)
class CopilotModelRate:
    """Per-1M-token rates for one Copilot model.

    All prices are USD per 1 million tokens. ``cache_write`` is only
    applicable to Anthropic models and GPT-5.6 Sol/Terra/Luna.
    """

    name: str
    input: float
    cached_input: float
    output: float
    cache_write: float = 0.0


# Representative subset of the Copilot model catalog (per-1M-token USD).
# The full table lives in docs/github-copilot-pricing.md.
COPILOT_MODEL_RATES: dict[str, CopilotModelRate] = {
    "gpt-5-mini": CopilotModelRate("GPT-5 mini", 0.25, 0.025, 2.00),
    "gpt-5.4-nano": CopilotModelRate("GPT-5.4 nano", 0.20, 0.02, 1.25),
    "gpt-5.4-mini": CopilotModelRate("GPT-5.4 mini", 0.75, 0.075, 4.50),
    "gpt-5.4": CopilotModelRate("GPT-5.4", 2.50, 0.25, 15.00),
    "gpt-5.5": CopilotModelRate("GPT-5.5", 5.00, 0.50, 30.00),
    "gpt-5.6-luna": CopilotModelRate("GPT-5.6 Luna", 0.20, 0.02, 1.20, 0.25),
    "gpt-5.6-terra": CopilotModelRate("GPT-5.6 Terra", 2.00, 0.20, 12.00, 2.50),
    "gpt-5.6-sol": CopilotModelRate("GPT-5.6 Sol", 5.00, 0.50, 30.00, 6.25),
    "claude-haiku-4.5": CopilotModelRate("Claude Haiku 4.5", 1.00, 0.10, 5.00, 1.25),
    "claude-sonnet-4.5": CopilotModelRate("Claude Sonnet 4.5", 3.00, 0.30, 15.00, 3.75),
    "claude-opus-4.5": CopilotModelRate("Claude Opus 4.5", 5.00, 0.50, 25.00, 6.25),
    "claude-sonnet-5": CopilotModelRate("Claude Sonnet 5 (promo)", 2.00, 0.20, 10.00, 2.50),
    "gemini-3.5-flash": CopilotModelRate("Gemini 3.5 Flash", 1.50, 0.15, 9.00),
    "gemini-3.6-flash": CopilotModelRate("Gemini 3.6 Flash", 1.50, 0.15, 7.50),
    "grok-4.5": CopilotModelRate("Grok 4.5", 2.00, 0.50, 6.00),
    "kimi-k2.7-code": CopilotModelRate("Kimi K2.7 Code", 0.95, 0.19, 4.00),
}


def get_copilot_plan(name: str) -> Optional[CopilotPlan]:
    """Look up a Copilot plan by its canonical key."""
    return COPILOT_PLANS.get(name)


def list_copilot_plans() -> list[str]:
    """List all registered Copilot plan keys."""
    return sorted(COPILOT_PLANS.keys())


def estimate_credits(
    model: str,
    input_tokens: float,
    output_tokens: float,
    cached_input_tokens: float = 0.0,
    cache_write_tokens: float = 0.0,
) -> float:
    """Estimate AI credits consumed by one interaction with a model.

    Token counts are converted to dollars at the model's per-1M-token rates,
    then to credits at 1 credit = $0.01.

    Args:
        model: A key from :data:`COPILOT_MODEL_RATES`.
        input_tokens: Fresh input tokens.
        output_tokens: Generated output tokens.
        cached_input_tokens: Cached (reused) input tokens.
        cache_write_tokens: Cache-write tokens (Anthropic, GPT-5.6 family).

    Returns:
        Estimated AI credits consumed (dollars × 100).

    Raises:
        KeyError: If ``model`` is not in :data:`COPILOT_MODEL_RATES`.
    """
    rate = COPILOT_MODEL_RATES[model]
    cost_usd = (
        input_tokens / 1_000_000 * rate.input
        + cached_input_tokens / 1_000_000 * rate.cached_input
        + cache_write_tokens / 1_000_000 * rate.cache_write
        + output_tokens / 1_000_000 * rate.output
    )
    return cost_usd / CREDIT_VALUE_USD


def copilot_plan_cost(plan: CopilotPlan, credits_used: float) -> float:
    """Monthly cost for a plan given credits consumed.

    Subscription price plus overage: credits above the included allowance
    billed at $0.01 each. Credits at or below the allowance cost nothing
    extra.

    Args:
        plan: The Copilot plan.
        credits_used: Total AI credits consumed in the month.

    Returns:
        Total monthly cost in USD.
    """
    overage = max(0.0, credits_used - plan.included_credits)
    return plan.price_usd + overage * CREDIT_VALUE_USD


def compute_copilot_cost(
    plan: CopilotPlan, credits_used: float, seats: int = 1
) -> dict[str, float]:
    """Full cost breakdown for a Copilot plan.

    For per-user plans (Business, Enterprise), the subscription and the
    included allowance scale by the number of seats; the allowance is pooled
    across the org, so ``credits_used`` is the org-wide total.

    Args:
        plan: The Copilot plan.
        credits_used: Total AI credits consumed in the month (org-wide for
            per-user plans).
        seats: Number of seats (per-user plans only; ignored otherwise).

    Returns:
        Dict with ``subscription``, ``included_credits``, ``overage_credits``,
        ``overage_cost``, and ``total`` (all USD except the credit counts).
    """
    if plan.per_user:
        subscription = plan.price_usd * seats
        included = plan.included_credits * seats
    else:
        subscription = plan.price_usd
        included = plan.included_credits

    overage_credits = max(0.0, credits_used - included)
    overage_cost = overage_credits * CREDIT_VALUE_USD
    return {
        "subscription": round(subscription, 6),
        "included_credits": included,
        "overage_credits": round(overage_credits, 6),
        "overage_cost": round(overage_cost, 6),
        "total": round(subscription + overage_cost, 6),
    }
