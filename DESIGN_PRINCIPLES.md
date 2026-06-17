# Design Principles

## 1. Usage is derived, not specified

Resource consumption flows from high-level parameters (traffic, frequency, payload size) through a dependency graph. Individual resource usage is never a free variable — it is computed from the graph topology and propagation rules.

> Academic basis: "Workload derivation" (CostHat, UCC 2016); "Symbolic cost expressions" parameterized by input features (Skyler, ASPLOS 2026).

## 2. The model is a directed acyclic graph

Services, functions, or infrastructure components are nodes. Edges represent invocation or data dependencies, weighted by call frequency and data-size ratios. The graph must be a DAG — cycles in cost derivation are undefined.

> Academic basis: "Service call graph" (CostHat); "Workload dependency graph" (GARMA); "Serverless Economic Graph" (Skyler).

## 3. Cost propagation is compositional

The cost of the system is the sum of derived costs for each node. Each node's cost depends on its incoming workload and its per-unit cost factors. Propagation is bottom-up: from leaves (external-facing services) through internal dependencies.

> Academic basis: Cost propagation in CostHat §2.4; CostHat Equation 2: `C(ζ) = Σ c(s,ζ)`.

## 4. Parameters are first-class citizens

Traffic rates, payload sizes, call frequencies, and other workload parameters are symbolic variables in the model. They can be varied for what-if analysis without re-deriving the graph structure.

> Academic basis: "Input-parameter sensitive function models" (Eismann et al., ICPE 2020); "Performance Model Parameters" (GARMA/Palladio).

## 5. The model is provider-agnostic

The dependency graph and usage derivation are independent of pricing. Provider-specific pricing is plugged in as a separate layer: derived usage × unit price = cost. This separates the *what you use* problem from the *what it costs* problem.

> Academic basis: Skyler's pluggable pricing plugins; CostHat's separation of workload model from cost model.

## 6. The model supports sensitivity analysis

Given the graph and parameters, the model must answer: "Which parameter changes affect cost the most?" and "What happens to total cost if traffic doubles?" This requires symbolic or parametric representation, not just point estimates.

> Academic basis: CostHat's what-if analysis (§4); Skyler's cost prediction queries (§6); GARMA's bounded best/worst-case estimation.

## 7. The model accommodates LLM token costs

Token consumption in LLM API calls follows the same propagation pattern as traditional cloud resources: input tokens flow into a node, processing produces output tokens, and those flow to downstream nodes. The model must not assume only traditional cloud metrics.

> This is a novel contribution; existing academic work does not explicitly model LLM token economics in DAG-based cost derivation.