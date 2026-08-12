<!-- These notes describe the Copilot pricing model research.
For the canonical price data, see prices.yaml in this directory. -->

# GitHub Copilot Pricing Research (June 2026 usage-based billing)

Research snapshot for modeling GitHub Copilot in the infra-cost-model DAG.
All prices verified against GitHub's published docs and blog as of August 2026.

## Billing model: GitHub AI Credits

Since **June 1, 2026**, all GitHub Copilot plans bill usage-based:

- Every model interaction consumes **tokens**: input, cached input, cache
  write (Anthropic + GPT-5.6 family), and output.
- Each token is priced per model; the dollar cost is converted to **GitHub AI
  Credits** at a fixed rate of **1 credit = $0.01 USD**.
- Each plan includes a **monthly credit allowance**; overage is billed at the
  per-credit rate ($0.01/credit).
- **Code completions and next-edit suggestions are NOT billed in credits** and
  remain unlimited on all paid plans.
- Included credits do not carry over; the allowance resets at 00:00 UTC on
  the 1st of each month.

Sources:
- https://docs.github.com/en/copilot/concepts/billing/usage-based-billing-for-individuals
- https://docs.github.com/en/copilot/concepts/billing/usage-based-billing-for-organizations-and-enterprises
- https://github.blog/news-insights/company-news/github-copilot-is-moving-to-usage-based-billing/

## Plans

| Plan | Price | Included AI credits / month | Notes |
| --- | --- | --- | --- |
| Free | $0 | small allowance (auto model selection only) | 2,000 completions + 50 chat requests/mo |
| Pro | $10 | 1,500 (1,000 base + 500 flex) | individual |
| Pro+ | $39 | 7,000 (3,900 base + 3,100 flex) | individual |
| Max | $100 | 20,000 (10,000 base + 10,000 flex) | individual |
| Business | $19/user | 1,900 / user | pooled org-wide; promo 3,000 Jun 1–Sep 1, 2026 |
| Enterprise | $39/user | 3,900 / user | pooled org-wide; promo 7,000 Jun 1–Sep 1, 2026; requires GitHub Enterprise Cloud |

Individual plans split the allowance into **base credits** (match the
subscription price, never change) and a **flex allotment** (variable monthly
top-up). Org plans pool per-user allowances at the billing-entity level: an
org with 100 Business seats shares one 190,000-credit pool.

## Per-model token rates (USD per 1M tokens)

Representative subset (full table at
https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing):

| Model | Input | Cached input | Cache write | Output |
| --- | --- | --- | --- | --- |
| GPT-5 mini | $0.25 | $0.025 | — | $2.00 |
| GPT-5.4 nano | $0.20 | $0.02 | — | $1.25 |
| GPT-5.4 mini | $0.75 | $0.075 | — | $4.50 |
| GPT-5.4 | $2.50 | $0.25 | — | $15.00 |
| GPT-5.5 | $5.00 | $0.50 | — | $30.00 |
| GPT-5.6 Luna | $0.20 | $0.02 | $0.25 | $1.20 |
| GPT-5.6 Terra | $2.00 | $0.20 | $2.50 | $12.00 |
| GPT-5.6 Sol | $5.00 | $0.50 | $6.25 | $30.00 |
| Claude Haiku 4.5 | $1.00 | $0.10 | $1.25 | $5.00 |
| Claude Sonnet 4.5 | $3.00 | $0.30 | $3.75 | $15.00 |
| Claude Opus 4.5 | $5.00 | $0.50 | $6.25 | $25.00 |
| Claude Sonnet 5 (promo) | $2.00 | $0.20 | $2.50 | $10.00 |
| Gemini 3.5 Flash | $1.50 | $0.15 | — | $9.00 |
| Gemini 3.6 Flash | $1.50 | $0.15 | — | $7.50 |
| Grok 4.5 | $2.00 | $0.50 | — | $6.00 |
| Kimi K2.7 Code | $0.95 | $0.19 | — | $4.00 |

Notes:
- Output is 5–8× the input rate — the dominant cost driver.
- Cached input bills at ~10% of fresh input.
- Long-context tiers (input > 200K/272K tokens) roughly double input and
  output rates.
- Copilot code review additionally consumes GitHub Actions minutes (billed
  separately, attributed to the repository).
- Paid individual plans get a 10% discount on model costs when using auto
  model selection.

## Cost model

For a plan with subscription price `P`, included allowance `C`, and monthly
credit consumption `U`:

```
monthly_cost = P + max(0, U − C) × $0.01
```

For per-user org plans with `S` seats: `P = price × S`, `C = credits_per_user × S`
(pooled org-wide; `U` is the org-wide consumption).

## How this maps to infra-cost-model

- **`credit_pool` SaaS shape** (`infra_cost_model/saas/pricing_shapes.py`):
  `subscription + max(0, quantity − includedCredits) × creditValue`. The
  existing shapes cannot express this: `free_tier` applies its allowance per
  metric, while Copilot's allowance is a dollar-denominated pool applied
  against aggregate consumption.
- **`vendors/github-copilot/prices.yaml`**: canonical plan pricing and allowance
  data consumed by the catalog.
- **`examples/github-copilot.yaml`**: 25-seat Copilot Business org priced via
  the shape, with `creditsUsed` as a symbolic parameter for what-if analysis.
