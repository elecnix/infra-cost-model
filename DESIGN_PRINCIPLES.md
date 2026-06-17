# Design Principles

> **Terminology note:** This document uses the canonical terms defined in [UBIQUITOUS_LANGUAGE.md](./UBIQUITOUS_LANGUAGE.md). When in doubt about a term, refer there.

## 1. Usage is derived, not specified

Resource consumption flows from high-level parameters (traffic, frequency, payload size) through a dependency graph. Individual resource usage is never a free variable — it is computed from the graph topology and propagation rules.

> Academic basis: "Workload derivation" (CostHat, UCC 2016); "Symbolic cost expressions" parameterized by input features (Skyler, ASPLOS 2026).

## 2. The model is a directed acyclic graph

Services, functions, or infrastructure components are nodes. Edges represent invocation or data dependencies, weighted by call frequency and data-size ratios. The graph must be a DAG — cycles in cost derivation are undefined.

> Academic basis: "Service call graph" (CostHat); "Workload dependency graph" (GARMA); "Serverless Economic Graph" (Skyler).

## 3. Cost propagation is compositional

The cost of the system is the sum of derived costs for each node. Each node's cost depends on its incoming workload and its per-unit cost factors. Propagation is bottom-up: from entry points through internal dependencies.

> Academic basis: Cost propagation in CostHat §2.4; CostHat Equation 2: `C(ζ) = Σ c(s,ζ)`.

## 4. Parameters are first-class citizens

Traffic rates, payload sizes, call frequencies, and other workload parameters are symbolic variables in the model. They can be varied for what-if analysis without re-deriving the graph structure.

> Academic basis: "Input-parameter sensitive function models" (Eismann et al., ICPE 2020); "Performance Model Parameters" (GARMA/Palladio).

## 5. Two inputs, one engine

The cost engine joins two separate IRs:

- **Resource IR** — what infrastructure exists (from parsing .tf files, Pulumi exports, or CDK synth). Contains resource types, configurations, regions, and attributes.
- **Cost Model IR** — how infrastructure is used (from YAML, TypeScript, or Python SDK). Contains DAG topology, call frequencies, and per-node usage metrics.

Changing your infrastructure (add an RDS instance) changes the Resource IR. Changing your assumptions (double traffic, add a region) changes the Cost Model IR. The engine joins them: `Cost Model IR × Resource IR → derived usage × pricing = cost`.

This means you can run the same cost model against different environments (dev, staging, prod) or run different scenarios (1 user vs 100K) against the same infrastructure.

## 6. The model is provider-agnostic

The dependency graph and usage derivation are independent of pricing. Provider-specific pricing is plugged in as a separate layer: derived usage × unit price = cost. This separates the *what you use* problem from the *what it costs* problem.

> Academic basis: Skyler's pluggable pricing plugins; CostHat's separation of workload model from cost model.

## 7. The model supports sensitivity analysis

Given the graph and parameters, the model must answer: "Which parameter changes affect cost the most?" and "What happens to total cost if traffic doubles?" This requires symbolic or parametric representation, not just point estimates.

> Academic basis: CostHat's what-if analysis (§4); Skyler's cost prediction queries (§6); GARMA's bounded best/worst-case estimation.

## 8. The model accommodates LLM token costs

Token consumption in LLM API calls follows the same propagation pattern as traditional cloud resources: input tokens flow into a node, processing produces output tokens, and those flow to downstream nodes. The model must not assume only traditional cloud metrics.

> This is a novel contribution; existing academic work does not explicitly model LLM token economics in DAG-based cost derivation.

## 9. DAG-first UX with flat overrides as escape hatch

The DAG is the primary interface. Flat per-resource overrides exist for migration and edge cases but are explicitly discouraged:

- The DAG syntax (`→ resource: rate`) reads like traffic flow, not a spreadsheet
- Entry point + frequency drives all derived usage — one number change cascades
- Flat overrides require specifying every resource's usage independently
- Validation warns when flat overrides conflict with a known call chain
- A `graph` command renders the DAG visually

> The anti-pattern is Infracost's `infracost-usage.yml`, where every usage value is a manual override with no relationships between resources.

## 10. Type-safe SDK from IaC code generation

The SDK generates types from your infrastructure definition, so you cannot reference non-existent resources or attributes:

```
.tf files → terraform providers schema -json ──→ codegen → types.ts
Pulumi   → pulumi stack export --json           ──→ codegen → types.ts
CDK      → cdk synth                            ──→ codegen → types.ts
```

Generated types provide:

- **ResourceAddress** — union type of all resource addresses in your project (autocomplete + compile-time errors)
- **Per-resource usage params** — a Lambda node accepts `compute`, `memory`, `invocations`; a DynamoDB node accepts `readUnits`, `writeUnits`, `storageGb`
- **Node type metadata** — compute nodes can call things; storage nodes are leaves; routing nodes can call compute nodes

```typescript
// ✅ Valid — resource exists, usage params match node type
api.calls("aws_lambda_function.get_user", [
  { to: "aws_dynamodb_table.users", rate: 1, type: "read" },
]);

// ❌ Compile error — resource doesn't exist in your .tf files
api.calls("aws_s3_bucket.oops", []);

// ❌ Compile error — storageGb is not a Lambda usage param
api.usage("aws_lambda_function.get_user", { storageGb: "10" });
```

Code generation is the standard approach (CDKTF, Pulumi, AWS CDK all do it). No compiler extensions needed.

## 11. Three surfaces, one schema

Users declare cost models through three interfaces, all producing the same Cost Model IR:

| Interface | For | Strengths |
|-----------|-----|-----------|
| **YAML** | Terraform users, quick sketches, CI pipelines | Familiar, diffable, easy in reviews |
| **TypeScript SDK** | Pulumi/CDK users, complex scenarios | Type-safe, programmatic what-if loops, integrates with IaC code |
| **Python SDK** | Data teams, Jupyter notebooks, automation | Scriptable, pandas integration, sensitivity analysis |

All three share a JSON Schema as the single source of truth. The YAML validates against it. The SDKs generate types from it. The engine consumes it.

```
                    ┌──────────────────┐
                    │   Cost Engine    │
                    └────────┬─────────┘
                             │
                    ┌────────▼─────────┐
                    │ Cost Model IR    │
                    │ (JSON Schema)    │
                    └────────┬─────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
     ┌────────▼───┐  ┌──────▼──────┐  ┌──▼──────────┐
     │ YAML file   │  │ TS SDK      │  │ Python SDK   │
     │ (validates  │  │ (codegen    │  │ (codegen     │
     │  against    │  │  from .tf)  │  │  from .tf)   │
     │  schema)    │  │             │  │              │
     └─────────────┘  └─────────────┘  └──────────────┘
```

The YAML syntax for DAG definition:

```yaml
workflow: my-api
entry: aws_api_gateway_rest_api.my_api
frequency: 1000/min

calls:
  aws_api_gateway_rest_api.my_api:
    data_out: 50KB
    → aws_lambda_function.get_user: 0.8
    → aws_lambda_function.create_user: 0.2

  aws_lambda_function.get_user:
    compute: 200ms
    memory: 256MB
    → aws_dynamodb_table.users:
        rate: 1
        type: read

  aws_lambda_function.create_user:
    compute: 350ms
    memory: 512MB
    → aws_dynamodb_table.users:
        rate: 1
        type: write
```

The TypeScript SDK mirrors the YAML one-to-one:

```typescript
const api = Workflow.fromTf("my-api", "./infra/", {
  entry: "aws_api_gateway_rest_api.my_api",
  frequency: perMinute(1000),
});

api.calls("aws_api_gateway_rest_api.my_api", [
  { to: "aws_lambda_function.get_user", rate: 0.8 },
  { to: "aws_lambda_function.create_user", rate: 0.2 },
]);
```

The Python SDK mirrors both:

```python
api = Workflow.from_tf("my-api", "./infra/",
    entry="aws_api_gateway_rest_api.my_api",
    frequency=per_minute(1000),
)

api.calls("aws_api_gateway_rest_api.my_api", [
    Call(to="aws_lambda_function.get_user", rate=0.8),
    Call(to="aws_lambda_function.create_user", rate=0.2),
])
```

Same mental model, three surfaces. Learn one, know all three.