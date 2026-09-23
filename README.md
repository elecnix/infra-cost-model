# infra-cost-model

DAG-based infrastructure cost modeling: deriving resource consumption from higher-level parameters through dependency graphs.

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/elecnix/infra-cost-model)

## Why

Existing infrastructure-as-code cost tools (Infracost, terracost, OpenInfraQuote) treat usage as static overrides — you manually specify "1000 requests/month" for each resource. This breaks down when resource consumption is *derived* from traffic flowing through a dependency graph of services.

This project specifies a model where usage flows through the graph: a frontend receiving 10k requests/sec propagates demand to downstream services based on call frequency and data dependencies. Cost is then computed from derived usage × pricing.

## Core Concepts

- **Workload derivation**: Per-service workload is computed recursively from inbound traffic through a weighted service call graph (Leitner, Cito & Stöckli, *UCC 2016* — "CostHat")
- **Economic sinks**: Points in the graph where cloud API invocations directly contribute to cost (Ribeiro et al., *ASPLOS 2026* — "Skyler")
- **Symbolic cost expressions**: Cost formulas parameterized by input features, enabling what-if analysis without re-derivation (Skyler, ibid.)
- **What-if & sensitivity analysis**: Exploring cost impact of workload changes or architectural decisions before implementation (CostHat; Skyler)

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

The bundled `data/seed/aws_pricelist_seed.json` is a small us-east-1 fixture used by the test suite only — it is **not** a setup step for users, and `seed-pricing` exists purely for offline/testing.

## Metrics with no price

The engine prices a usage metric from its `shape`, then from the catalog, then from the node's `pricingRates`. If all three come up empty, the node's cost leaves that metric out. Each command that computes costs prints one warning per such metric on stderr, with the node, the metric, the provider, service and region, and the quantity left out. `analyze --json` also lists them under `unpriced_metrics`. A metric with a quantity of 0 doesn't warn.

Exit codes stay 0. To fail a CI run instead, pass `--exit-on-unpriced` to `compute` or `analyze`. Python callers can read `CostEngine.unpriced_metrics` after `compute()`, and each metric also raises an `UnpricedMetricWarning` through the `warnings` module.

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
