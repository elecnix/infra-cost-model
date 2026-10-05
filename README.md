# infra-cost-model

DAG-based infrastructure cost modeling: deriving resource consumption from higher-level parameters through dependency graphs.

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/elecnix/infra-cost-model)

## Why

Existing infrastructure-as-code cost tools (Infracost, terracost, OpenInfraQuote) treat usage as static overrides — you manually specify "1000 requests/month" for each resource. This breaks down when resource consumption is *derived* from traffic flowing through a dependency graph of services.

This project specifies a model where usage flows through the graph: a frontend receiving 10k requests/sec propagates demand to downstream services based on call frequency and data dependencies. Cost is then computed from derived usage × pricing.

## Core Concepts

- **Workload derivation**: Per-service workload is computed recursively from inbound traffic through a weighted service call graph (Leitner, Cito & Stöckli, *UCC 2016* — "CostHat")
- **Billable nodes**: each **node** in the graph adds cost through its **derived usage**. Ribeiro et al. (*ASPLOS 2026*, "Skyler") call a point where a cloud API call adds cost directly an "economic sink". This project says **node** instead, and the [glossary](UBIQUITOUS_LANGUAGE.md) lists "economic sink" as a term to avoid.
- **Symbolic cost expressions**: Cost formulas parameterized by input features, enabling what-if analysis without re-derivation (Skyler, ibid.)
- **What-if & sensitivity analysis**: Exploring cost impact of workload changes or architectural decisions before implementation (CostHat; Skyler)

## Install

Install a release from PyPI:

```bash
pip install infra-cost-model
```

The first PyPI release is still pending. Until it's out, or to try code that isn't released yet, install from this repository:

```bash
pip install "git+https://github.com/elecnix/infra-cost-model"
```

Add `@<tag or commit>` to the URL to install a specific version.

## Quick Usage

```bash
# Validate a cost model
infra-cost-model validate model.yaml

# Compute costs
infra-cost-model compute model.yaml

# Full analysis with derived usage
infra-cost-model analyze model.yaml --json

# Parameter sweep across explicit values
infra-cost-model what-if model.yaml \
  --param frequency --values 1000,10000,100000,1000000

# A/B comparison of two architectures
infra-cost-model what-if model-a.yaml --compare model-b.yaml \
  --param frequency --values 1000,10000,100000

# Visualize the DAG
infra-cost-model graph model.yaml
```

## Metric values

A usage metric's `value` is the quantity it bills: how many requests an invocation makes, how many monthly active users a tenant carries. It is a number, one parameter name, or arithmetic over the workflow's parameters with `+ - * /` and parentheses:

```yaml
version: "1.0"

workflow:
  name: saas-identity
  entry: identity_provider
  frequency: { unit: perMonth, value: 1 }
  parameters:
    customers: 25

nodes:
  identity_provider:
    nodeType: external
    resourceAddress: kinde.tenant
    provider: kinde
    service: Kinde
    region: global
    usageMetrics:
      MAU:            { unit: users,       value: "customers * 40", fixed: true }
      SSO-Connection: { unit: connections, value: customers,        fixed: true }
```

Here `MAU` is 1,000 monthly active users for 25 customers, and `SSO-Connection` is one SSO connection per customer. Because the expressions name `customers`, `what-if --param customers` and a sensitivity run over it move both metrics without a pre-processing step that multiplies values and strips a marker before the engine sees the model.

An unknown parameter name, an operator outside `+ - * /`, and a division by zero each raise an error naming the expression. The expression is arithmetic, never code: a call or a conditional in a `value` is refused. Frequency and edge `rate` still take one number or one parameter name.

## Pinning the engine version

A model may name the engine version it needs:

```yaml
version: "1.0"
requiresEngine: ">=0.2.0"
```

The value is a [PEP 440](https://peps.python.org/pep-0440/#version-specifiers) specifier, so `>=0.2.0`, `==0.2.1`, and `>=0.2,<0.4` all work. When the running engine does not satisfy it, `compute`, `analyze`, `what-if`, and `sensitivity` exit non-zero and name both versions, and `validate` reports the mismatch as a validation error. There is no flag to bypass the check.

The pin matters because an engine too old for a model does not fail. It prices the fields it does not recognise at $0 and reports a total that reads as reasonable. A model using a SaaS `shape:` (see `examples/saas-subscription-api.yaml`) priced $0 for every shaped node on an engine from before that feature, and the run exited 0.

A model with no `requiresEngine` behaves exactly as it did before, so adding the field to an existing model is optional. Add it when the model uses a feature a reader's engine may predate.

## Pricing data

Prices are fetched live from the [Infracost Cloud Pricing API](https://www.infracost.io/docs/), covering all supported services and all regions:

```bash
# Authenticate once: set INFRACOST_API_KEY, or run `infracost auth login`
infra-cost-model sync-pricing                              # all services, all regions
infra-cost-model sync-pricing --region us-east-1 --region eu-west-1
```

The bundled `infra_cost_model/pricing/seed/seed_prices.json` is a small fixture, with AWS prices for us-east-1 and Azure prices for eastus, for the test suite and offline use. You don't need it to set up the tool. `seed-pricing` loads it into the local cache.

Every timestamp the cache writes states its UTC offset, so the same row reports the same age on any machine. `pricing-status` reports the cache and how old the newest row of each source is:

```bash
infra-cost-model pricing-status --json
infra-cost-model pricing-status --max-age-hours 24   # exits 1 when the fetched prices are older
```

The age gate watches the sources a sync fetched over the network — `infracost`, `azure-retail` and `aws-pricelist`. The bundled seed and vendor rows ship with a release rather than with a fetch, so they don't age out. `--max-age-hours` also fails when the cache holds no fetched rows at all. The `stale` and `maxAgeHours` fields appear in the JSON only when `--max-age-hours` is given, because without a limit there is no verdict to report.

### Pinning the price source

By default a run reads whichever source has a row: synced rows when the local cache holds them, the bundled rows otherwise, and a node's `pricingRates` when neither has one. The same model will then price from different sources on a laptop and in CI. `compute` can pin the source instead:

```bash
infra-cost-model compute model.yaml --pricing live    # synced rows only
infra-cost-model compute model.yaml --pricing seed    # bundled rows only
infra-cost-model compute model.yaml --pricing-db ./pricing.db
```

`--pricing live` prices from rows a sync wrote (`sync-pricing`). When a usage metric has no synced row, the run prints each missing metric on stderr and exits 1 without printing a total, so a CI job fails instead of reporting a number that mixed sources.

`--pricing seed` prices from the bundled seed file and vendor price files only. It reads a database of its own, never the local cache, so the number is the same on every machine, and the run leaves your cache as it found it. It refuses `--pricing-db`, which would contradict it.

`--pricing-db` names the cache file to read, so a CI job and a laptop can price from the same one. A path with no file there is an error rather than a silent drop back to the embedded rates.

All three need the catalog, so they can't be combined with `--no-catalog`.

## SaaS vendor prices

SaaS vendors such as WorkOS, Datadog and GitHub Copilot price from rows in `infra_cost_model/vendors/<id>/prices.yaml`. A node sets `provider` to the vendor id, and the catalog prices its metrics from those rows. To add a vendor, see [CONTRIBUTING.md](./CONTRIBUTING.md).

Version 0.3.0 removed the `free_tier`, `per_unit_flat` and `flat_subscription` pricing shapes ([#246](https://github.com/elecnix/infra-cost-model/issues/246)). `validate` rejects a model that still sets one, with a message that points to vendor price rows. CONTRIBUTING.md shows how to move each one to price rows.

## Node labels

A node may carry free-form `labels`. They change neither derived usage nor a price; they say how to slice a total:

```yaml
nodes:
  anthropic.messages.primary:
    labels: { category: llm }
  aws_lb.main:
    labels: { category: platform, env: prod }
  aws_tax.prod:
    labels: { category: tax }
  aws_s3.bucket:
    # no labels
```

`compute` and `analyze` take `--group-by <label>` to print one subtotal per value of that key, plus one for the nodes that carry none of it:

```bash
infra-cost-model compute model.yaml --monthly --group-by category
```

```text
Grouped by category:
  category=llm: $21.000000 (1 node(s))
  category=platform: $100.000000 (1 node(s))
  category=tax: $50.000000 (1 node(s))
  (unlabeled): $5.000000 (1 node(s))
```

`--exclude-label <key=value>` (repeatable) leaves the matching nodes out of the costs and of the total, and `--budget` then applies to what is left — a ceiling on the platform spend with the third-party API set apart from it. The command still prints the cost it left out, so a filtered total never reads as the whole model's spend:

```bash
infra-cost-model compute model.yaml --monthly --exclude-label category=llm --budget 160
```

```text
Costs for: model (pricing: embedded pricing rates, monthly)
----------------------------------------
  api: $100.000000
  bucket: $5.000000
  tax: $50.000000

Excluded by label category=llm ($21.000000):
  llm: $21.000000
----------------------------------------
Total Monthly Cost: $155.000000
```

`analyze --json` adds a `groups` list for `--group-by` and an `excluded_nodes` list for `--exclude-label`. A label value is a string and a label key no node in the model carries is almost always a typo, so either flag fails on one and names the keys the model does use. (Only these flags read labels: `compute` without them stays as lenient about a label as it already is about a missing `provider`.)

Either flag also fails with `--format json` on `compute`. A snapshot is the model's whole cost, meant to be committed and diffed ([#443](https://github.com/elecnix/infra-cost-model/issues/443)), so a filtered one would keep the excluded node while `--budget` gated the total without it. Run the two as separate commands.

## Metrics with no price

The engine prices a usage metric from its `shape`, then from the catalog, then from the node's `pricingRates`. If all three come up empty, the node's cost leaves that metric out. Each command that computes costs prints one warning per such metric on stderr, with the node, the metric, the provider, service and region, and the quantity left out. `analyze --json` also lists them under `unpriced_metrics`. A metric with a quantity of 0 doesn't warn.

Exit codes stay 0. To fail a CI run instead, pass `--exit-on-unpriced` to `compute` or `analyze`. Python callers can read `CostEngine.unpriced_metrics` after `compute()`, and each metric also raises an `UnpricedMetricWarning` through the `warnings` module.

## Diffable cost snapshots

`compute --format json` prints the same run as the table, arranged so you can commit it and read the diff:

```bash
infra-cost-model compute model.yaml --time-basis monthly --format json > cost-snapshot.json
```

```json
{
  "schemaVersion": 1,
  "engineVersion": "0.3.0",
  "timeBasis": "monthly",
  "total": 812.4,
  "nodes": {
    "gateway": {
      "total": 41.97,
      "fixed": 32.85,
      "variable": 9.12,
      "metrics": {
        "gatewayHours": {"quantity": 730, "unitPrice": 0.045, "cost": 32.85, "fixed": true, "priceSource": "seed"},
        "gatewayGb": {"quantity": 202.7, "unitPrice": 0.045, "cost": 9.12, "fixed": false, "priceSource": "seed"}
      }
    }
  },
  "unpriced": []
}
```

(The amounts above are made up, to show the format.)

Each metric reports the quantity the engine priced, the effective price of one unit of it, the cost, whether it is fixed, and where the price came from (`seed`, `infracost`, `azure-retail`, `vendor`, or the model's own `pricingRates`). When two machines disagree on a total, the snapshot points at the metric and the price source that moved. Keys are sorted, money is rounded to six decimals, and each cost is the engine's own, so a node's printed metrics reproduce its printed total and a diff is empty when nothing changed.

When nothing priced a metric, it stays out of `costs` and appears in `unpriced` instead, with the same node, metric, provider, service, region and quantity as the stderr warning.

The format is on `compute` rather than `analyze`, because `compute` has `--no-catalog` ([#420](https://github.com/elecnix/infra-cost-model/issues/420)) and so can say whether a run used the catalog or the model's embedded rates. `--format table` stays the default and is unchanged. `schemaVersion` rises when the snapshot's keys change.

## Blanket pricing for the long tail

Native handlers cover the resources whose usage the DAG derives from upstream flow. For the static, always-on tail (anything Infracost already prices), import an `infracost breakdown` instead of hand-writing a handler + descriptor:

```bash
infracost breakdown --path ./terraform --format json --out-file breakdown.json
infra-cost-model import-infracost breakdown.json > nodes.yaml   # priced flatOverride nodes
```

Each resource becomes a `flatOverride` node whose `fixed` metrics mirror Infracost's per-component monthly costs; compose the emitted `nodes` into a model alongside the DAG-derived handlers. Prefer a native handler where one exists — the import is the escape hatch (DP#9), not the default.

## References

- Leitner, Cito & Stöckli. "Modelling and Managing Deployment Costs of Microservice-Based Cloud Applications." *UCC 2016*. DOI: 10.1145/2996890.2996901
- Ribeiro et al. "Skyler: Static Analysis for Predicting API-Driven Costs in Serverless Applications." *ASPLOS 2026*. DOI: 10.1145/3779212.3790221
- Eismann et al. "Predicting the Costs of Serverless Workflows." *ICPE 2020*. DOI: 10.1145/3358960.3379133
- Böhme et al. "A Penny a Function: Towards Cost Transparent Cloud Programming." *arXiv:2309.04954*, 2023.
- Hummel et al. "GARMA: Generative Architectural Resource Demand Estimation for Microservice Applications." 2026.
- Khan et al. "Cost Modelling and Optimisation for Cloud: A Graph-Based Approach." *Journal of Cloud Computing* 13(1), 2024. DOI: 10.1186/s13677-024-00709-6
