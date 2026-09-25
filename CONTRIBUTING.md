# Contributing to Infra Cost Model

## Adding a SaaS vendor

To add a SaaS vendor (for example, Linear, Datadog, or Vercel):

1. Copy `infra_cost_model/vendors/_template/` to `infra_cost_model/vendors/<your-vendor>/` (use a lowercase id with hyphens).
2. Edit `vendor.yaml`: set `id`, `display_name`, `homepage`, and `pricing_page`.
3. Edit `prices.yaml`: add normalized price rows as described below.
4. Run `python3 -m infra_cost_model.cli validate <file>` for every cost model example you add or change.
5. Run `python3 -m pytest -q` to verify that the vendor loads correctly.
6. Open a pull request. A vendor-only contribution should touch only `infra_cost_model/vendors/<your-vendor>/`.

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

State `start_usage_amount` and `end_usage_amount` as a quantity per month, the way providers publish their allowances. The engine derives usage per second, so it scales a month of usage against the boundaries and then converts the cost to the output time basis ([#287](https://github.com/elecnix/infra-cost-model/issues/287)).

The boundaries apply once to the whole account. The engine adds up the monthly quantity of each metric across all nodes and workflows that share a provider, service and region, prices that total, and splits the cost across the nodes by quantity ([#294](https://github.com/elecnix/infra-cost-model/issues/294)). A pair of queues with 1,000,000 requests each shares one free allowance of 1,000,000 requests.

Some providers give a free allowance once to the whole account, across all regions. AWS does this for the first 100 GB of data transfer out, for the Lambda, SQS, SNS and KMS free requests, and for the CloudWatch free metrics, alarm metrics and log data. Azure does this for the free grant of the Functions consumption plan. `ACCOUNT_WIDE_FREE_TIERS` in `infra_cost_model/pricing/free_tiers.py` lists these metrics by vendor, service and `usage_metric`. For a listed metric, the engine applies the allowance once to the total of all regions and gives each region a part in proportion to its quantity. Each region pays its own rate for the rest ([#336](https://github.com/elecnix/infra-cost-model/issues/336)). A metric missing from the list gets one allowance per region. The list, not a price row field, marks an allowance as account-wide, so rows from the seed file, the vendor files and live sources get the same treatment.

A few providers give one free allowance to several metrics of a service. AWS gives 1,000,000 free SQS requests a month to standard and FIFO queues together, and 10,000,000 free CloudFront requests to HTTP and HTTPS together. `SHARED_FREE_ALLOWANCES` in the same file lists each such group with its vendor, service, metrics, allowance and unit. For a listed metric, the engine applies the group's allowance once to the total of all its metrics in all regions, and gives each pool a part in proportion to its quantity. Each pool pays its own metric's rate in its own region for the rest, from the first paid tier ([#338](https://github.com/elecnix/infra-cost-model/issues/338)). The table's allowance replaces the free tier in the metrics' rows. The seed file keeps a free row for each SQS metric, because it states the right price for a direct catalog query of one queue type.

A global service bills the account's use in every region together, at one price. AWS bills Route 53 hosted zones this way: $0.50 a month for each of the first 25 zones in the account, then $0.10. Standard queries to public hosted zones work the same way: $0.40 a million for the first billion queries a month, then $0.20 ([#384](https://github.com/elecnix/infra-cost-model/issues/384)). `GLOBAL_METRICS` in `infra_cost_model/pricing/global_services.py` lists such metrics by vendor, service and `usage_metric`. For a listed metric used in two or more regions, the engine prices the total of all regions once and splits the cost across the regions by quantity ([#378](https://github.com/elecnix/infra-cost-model/issues/378)). The rows under the region `global` price the total, or else the us-east-1 rows, or else the rows of one of the nodes' regions. A node in a region with no rows for a listed metric gets its price from the same rows. Add a metric only when the provider's pricing page confirms one price and one set of tiers for the whole account.

A resource that has no AWS region, such as a CloudFront distribution or a WAF web ACL with the scope `CLOUDFRONT`, gets the region `global`. The seed file prices it with rows under `global`. The Infracost Cloud Pricing API keeps these prices in its global catalog, with the usagetype prefix `Global-`. A descriptor with `global_scope` stores them under `global`, and `sync-pricing` syncs `global` after the AWS regions ([#385](https://github.com/elecnix/infra-cost-model/issues/385)).

The Infracost Cloud Pricing API states each paid price from 0 and leaves out the free allowances, which AWS publishes as separate "Global-" products. `FREE_ALLOWANCES` in the same file gives the monthly allowance of each metric that has a $0 tier in the seed file. When `sync-pricing` stores the live rows of a listed metric, it adds a $0 tier up to the allowance and starts the paid tiers there, so a live catalog prices the same usage as the seed catalog ([#356](https://github.com/elecnix/infra-cost-model/issues/356)). When you add a free tier to the seed file, add its allowance to `FREE_ALLOWANCES` as well. A test checks that the two agree.

Some allowances need more than a number ([#372](https://github.com/elecnix/infra-cost-model/issues/372), [#373](https://github.com/elecnix/infra-cost-model/issues/373)). Firestore gives a quota each day, so its entry is a day's quota times 30.4375, the days in an average month. GCP gives the Cloud Run free tier as a sum of money at Tier 1 prices. `SPEND_BASED_FREE_TIERS` states that price, and the sync gives a Tier 2 region fewer free units. Cloud Storage gives its free storage, operations and egress in three US regions only, so `FREE_ALLOWANCE_REGIONS` lists them, and the sync drops the product's $0 tier in the other regions ([#390](https://github.com/elecnix/infra-cost-model/issues/390)). Cloud Storage bills egress from every region on one SKU, so `PRICE_POOLS` prices all regions together, and the engine applies the free 100 GiB once, to the egress from the three regions ([#404](https://github.com/elecnix/infra-cost-model/issues/404)).

When the provider's pricing page states other tier bounds than the Infracost API, `TIER_BOUND_OVERRIDES` in `infra_cost_model/pricing/sources/infracost.py` gives both sets of bounds, the page, and the date you checked it. The sync then stores the page's bounds. It keeps the Infracost bounds once they no longer match the entry ([#391](https://github.com/elecnix/infra-cost-model/issues/391)).

When the Infracost API has no price for an Azure meter in a region, the sync reads the public Azure Retail Prices API, which Infracost copies ([#376](https://github.com/elecnix/infra-cost-model/issues/376)). A descriptor with `azure_retail` always reads that API. Its rows have the source `azure-retail`, and they replace seed rows the way Infracost rows do. Azure bills internet egress by zone. When a region lacks the egress meter in that API too, `AZURE_EGRESS_METER_FALLBACK` names a region of the same zone to read the meter from ([#392](https://github.com/elecnix/infra-cost-model/issues/392)).

A vendor directory and its `vendor.yaml` manifest define the canonical vendor identity. References in examples, provider registration, and price rows must use that identity consistently. `prices.yaml` is the canonical price data; nearby research notes may explain the model and cite sources but must not become a second price schedule.

## Development

- **Run tests:** `python3 -m pytest -q`
- **Validate a model:** `python3 -m infra_cost_model.cli validate <file>`
- **Build the documentation site:** `pip install -e ".[docs]"`, then `mkdocs build --strict` (or `mkdocs serve` to preview it). `docs/hooks.py` builds every page from the Markdown files at the repository root, the example models, the vendor pricing notes, and the command parser, so a change to any of them changes the site. Links between the root Markdown files use the file names, such as `DESIGN_PRINCIPLES.md`, so they work on GitHub and on the site. After a merge into `main`, `.github/workflows/docs.yml` deploys the site to GitHub Pages.
- **Required checks:** a PR merges into `main` only after `test (3.11)`, `test (3.12)`, `test (3.13)`, `ts`, `vendor-check`, and `wheel-install` pass. A repository ruleset in the GitHub settings sets this list.
- **Core documentation:**
  - [DESIGN_PRINCIPLES.md](./DESIGN_PRINCIPLES.md)
  - [UBIQUITOUS_LANGUAGE.md](./UBIQUITOUS_LANGUAGE.md)

## Releasing

Publishing a GitHub release starts `.github/workflows/publish.yml`, which builds the package and uploads it to PyPI.

1. Set the new version in `infra_cost_model/__init__.py` (`__version__`) and in `sdk/ts/package.json` in the same PR. `tests/test_sdk_version.py` fails when the two differ.
2. After that PR merges, create a GitHub release on `main` with the tag `vX.Y.Z`, where `X.Y.Z` matches `__version__`. If they differ, the workflow stops before it builds anything.

PyPI accepts the upload through trusted publishing, so the repository doesn't store a PyPI token. Before the first release, the repository owner sets this up once:

1. On pypi.org, add a trusted publisher for the `infra-cost-model` project (a "pending publisher" until the first upload creates the project). Use owner `elecnix`, repository `infra-cost-model`, workflow `publish.yml`, and environment `pypi`.
2. In the GitHub repository settings, under Environments, create an environment called `pypi`. You can add required reviewers there to approve each upload.
