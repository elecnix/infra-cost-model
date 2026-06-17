# infra-cost-model

DAG-based infrastructure cost modeling: deriving resource consumption from higher-level parameters through dependency graphs.

## Why

Existing IaC cost tools (Infracost, terracost, OpenInfraQuote) treat usage as static overrides — you manually specify "1000 requests/month" for each resource. This breaks down when resource consumption is *derived* from traffic flowing through a dependency graph of services.

This project specifies a model where usage flows through the graph: a frontend receiving 10k requests/sec propagates demand to downstream services based on call frequency and data dependencies. Cost is then computed from derived usage × pricing.

## Core Concepts

- **Workload derivation**: Per-service workload is computed recursively from inbound traffic through a weighted service call graph (Leitner, Cito & Stöckli, *UCC 2016* — "CostHat")
- **Economic sinks**: Points in the graph where cloud API invocations directly contribute to cost (Ribeiro et al., *ASPLOS 2026* — "Skyler")
- **Symbolic cost expressions**: Cost formulas parameterized by input features, enabling what-if analysis without re-derivation (Skyler, ibid.)
- **What-if & sensitivity analysis**: Exploring cost impact of workload changes or architectural decisions before implementation (CostHat; Skyler)

## References

- Leitner, Cito & Stöckli. "Modelling and Managing Deployment Costs of Microservice-Based Cloud Applications." *UCC 2016*. DOI: 10.1145/2996890.2996901
- Ribeiro et al. "Skyler: Static Analysis for Predicting API-Driven Costs in Serverless Applications." *ASPLOS 2026*. DOI: 10.1145/3779212.3790221
- Eismann et al. "Predicting the Costs of Serverless Workflows." *ICPE 2020*. DOI: 10.1145/3358960.3379133
- Böhme et al. "A Penny a Function: Towards Cost Transparent Cloud Programming." *arXiv:2309.04954*, 2023.
- Hummel et al. "GARMA: Generative Architectural Resource Demand Estimation for Microservice Applications." 2026.
- Khan et al. "Cost Modelling and Optimisation for Cloud: A Graph-Based Approach." *Journal of Cloud Computing* 13(1), 2024. DOI: 10.1186/s13677-024-00709-6