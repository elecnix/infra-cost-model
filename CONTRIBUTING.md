# Contributing to Infra Cost Model

## Adding a SaaS vendor

To add a SaaS vendor (for example, Linear, Datadog, or Vercel):

1. Copy `vendors/_template/` to `vendors/<your-vendor>/` (use a lowercase id with hyphens).
2. Edit `vendor.yaml`: set `id`, `display_name`, `homepage`, and `pricing_page`.
3. Edit `prices.yaml`: add normalized price rows as described below.
4. Run `python3 -m infra_cost_model.cli validate <file>` for every cost model example you add or change.
5. Run `python3 -m pytest -q` to verify that the vendor loads correctly.
6. Open a pull request. A vendor-only contribution should touch only `vendors/<your-vendor>/`.

Two vendor-only pull requests do not conflict because each changes prices in its own directory.

## Price row reference

Each entry in `prices.yaml` represents one flat price or one tier. Fields use the same snake-case names as the `Price` dataclass:

- `vendor` (required): Canonical provider identity used by cost model nodes and catalog queries. It must be a lowercase id matching `^[a-z][a-z0-9_-]*$`. Use the provider's stable vendor identity, not a product, plan, display name, or reseller name. Normally this is the `id` in the directory's `vendor.yaml`; use a different value only when the row intentionally belongs to another canonical provider identity.
- `service` (required): Stable service or product identifier within the vendor catalog.
- `region` (required): Pricing region. Use `global` only when the vendor publishes one location-independent price.
- `product_family` (optional): Provider product-family classification when needed to distinguish otherwise similar offers.
- `attributes` (optional): Provider-specific dimensions that identify the priced offer. Use an empty mapping when there are none.
- `usage_metric` (required): Canonical metric consumed by catalog queries and the cost model's usage derivation layer.
- `unit` (required): Unit to which `price_usd` applies, such as `request`, `GB`, or `month`.
- `price_usd` (required): Price in United States dollars per `unit` within this row's tier. A zero price represents a free tier.
- `start_usage_amount` (optional): Inclusive lower tier boundary. Use `0` for the first bounded tier. For a free tier, pair `start_usage_amount: 0` with a positive `end_usage_amount` and `price_usd: 0`; the paid overage row starts at that same end boundary.
- `end_usage_amount` (optional): Exclusive upper tier boundary. Omit it for the final open-ended overage tier. Adjacent tiers should share boundaries without gaps or overlaps.
- `purchase_option` (optional): Provider purchase or commitment option when it distinguishes prices for the same metric.
- `per` (optional): Name of the cost model parameter that scales this row's tier boundaries. For example, `per: seats` multiplies both `start_usage_amount` and `end_usage_amount` by the resolved `seats` value. It does not multiply `price_usd`; quantities above the scaled boundary are priced normally.
- `effective_date` (optional): Date from which the published price applies, in `YYYY-MM-DD` form. Record the provider's effective date rather than the date the row was added. Update it whenever the canonical price changes.
- `source` (optional): Provenance for the price, preferably the provider's authoritative pricing or billing documentation URL. Every manually maintained price should be traceable to such a source; do not use an aggregator when first-party documentation exists.
- `fetched_at` (optional): Timestamp when an automatically fetched price was retrieved. This is cache metadata and is normally omitted from hand-maintained vendor files.

A vendor directory and its `vendor.yaml` manifest define the canonical vendor identity. References in examples, provider registration, and price rows must use that identity consistently. `prices.yaml` is the canonical price data; nearby research notes may explain the model and cite sources but must not become a second price schedule.

## Development

- **Run tests:** `python3 -m pytest -q`
- **Validate a model:** `python3 -m infra_cost_model.cli validate <file>`
- **Core documentation:**
  - [DESIGN_PRINCIPLES.md](./DESIGN_PRINCIPLES.md)
  - [UBIQUITOUS_LANGUAGE.md](./UBIQUITOUS_LANGUAGE.md)
