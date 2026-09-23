<!-- These notes describe the Copilot pricing model research.
For the canonical price data, see prices.yaml in this directory. -->

# GitHub Copilot pricing research (June 2026 usage-based billing)

Research snapshot for modeling GitHub Copilot in the infra-cost-model DAG.
All prices verified against GitHub's published docs and blog as of August 2026.

## Billing model: GitHub AI credits

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

`prices.yaml` has the subscription price and the included credit allowance for each plan. This table lists the metric that each plan uses.

| Plan | Subscription metric | Credit metric | Allowance scales with |
| --- | --- | --- | --- |
| Pro | `Copilot-Pro-Month` | `Copilot-Pro-Credit` | fixed |
| Pro+ | `Copilot-Pro-Plus-Month` | `Copilot-Pro-Plus-Credit` | fixed |
| Max | `Copilot-Max-Month` | `Copilot-Max-Credit` | fixed |
| Business | `Copilot-Seat-Month` | `Copilot-Credit` | `seats` |
| Enterprise | `Copilot-Enterprise-Seat-Month` | `Copilot-Enterprise-Credit` | `seats` |

Copilot Free has no rows. It has no subscription price, and GitHub doesn't publish a fixed credit allowance for it. Free is limited to 2,000 code completions and 50 chat requests each month. Copilot Enterprise requires GitHub Enterprise Cloud.

The Business and Enterprise promotional allowances for existing customers ended on September 1, 2026. The rows use the standard allowances.

Individual plans split the allowance into **base credits** and a **flex allotment**. The base credits match the subscription price and don't change. GitHub can change the flex allotment from month to month. The individual-plan rows use the full allowance (base plus flex), so a lower flex allotment makes the real overage larger than the estimate. Organization plans pool per-user allowances at the billing-entity level. An organization with 100 Business seats shares one pool of 190,000 credits.

## Per-model token rates (USD per 1M tokens)

Representative subset (full table at
https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing):

| Model | Input | Cached input | Cache write | Output |
| --- | --- | --- | --- | --- |
| GPT-5 mini | $0.25 | $0.025 | n/a | $2.00 |
| GPT-5.4 nano | $0.20 | $0.02 | n/a | $1.25 |
| GPT-5.4 mini | $0.75 | $0.075 | n/a | $4.50 |
| GPT-5.4 | $2.50 | $0.25 | n/a | $15.00 |
| GPT-5.5 | $5.00 | $0.50 | n/a | $30.00 |
| GPT-5.6 Luna | $0.20 | $0.02 | $0.25 | $1.20 |
| GPT-5.6 Terra | $2.00 | $0.20 | $2.50 | $12.00 |
| GPT-5.6 Sol | $5.00 | $0.50 | $6.25 | $30.00 |
| Claude Haiku 4.5 | $1.00 | $0.10 | $1.25 | $5.00 |
| Claude Sonnet 4.5 | $3.00 | $0.30 | $3.75 | $15.00 |
| Claude Opus 4.5 | $5.00 | $0.50 | $6.25 | $25.00 |
| Claude Sonnet 5 (promo) | $2.00 | $0.20 | $2.50 | $10.00 |
| Gemini 3.5 Flash | $1.50 | $0.15 | n/a | $9.00 |
| Gemini 3.6 Flash | $1.50 | $0.15 | n/a | $7.50 |
| Grok 4.5 | $2.00 | $0.50 | n/a | $6.00 |
| Kimi K2.7 Code | $0.95 | $0.19 | n/a | $4.00 |

Notes:
- Output costs 5 to 8 times the input rate, and it's the largest part of the cost.
- Cached input bills at ~10% of fresh input.
- Long-context tiers (input > 200K/272K tokens) roughly double input and
  output rates.
- Copilot code review also consumes GitHub Actions minutes (billed
  separately, attributed to the repository).
- Paid individual plans get a 10% discount on model costs when using auto
  model selection.

## Cost model

For a plan with subscription price `P`, included allowance `C`, and monthly
credit consumption `U`:

```
monthly_cost = P + max(0, U − C) × $0.01
```

For per-user organization plans with `S` seats, `P = price × S` and `C = credits_per_user × S`.
The organization shares one pool, and `U` is the consumption of the whole organization.

## Price rows and example model

- **`prices.yaml`**: each plan has a subscription row and two credit rows. The first credit row is free up to the included allowance. The second bills each credit above the allowance at $0.01. On the organization plans, `per: seats` multiplies the allowance by the `seats` parameter. The catalog computes `P + max(0, U − C) × $0.01` from these rows, so the model doesn't need a special pricing shape.
- **`examples/github-copilot.yaml`**: a Copilot Business organization. It has 25 seats and uses 60,000 credits each month.
