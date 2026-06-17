# Ubiquitous Language

## Graph structure

| Term | Definition | Aliases to avoid |
|------|-----------|-----------------|
| **Node** | A billable infrastructure component in the DAG (e.g., a Lambda function, a DynamoDB table) | Action, ActionNode, ResourceSlot, vertex |
| **Edge** | A directed connection between two nodes representing invocation or data flow | Call, dependency, link, arc |
| **Entry node** | The node that receives external traffic; the starting point of cost propagation | Entry point, external-facing service, root, source |
| **Leaf node** | A node with no outgoing edges; it consumes resources but does not invoke other nodes | Terminal node, sink, economic sink |
| **Flow** | A complete path from an entry node through the DAG, representing one unit of traffic through the system | Request path, call chain, trace |

## Derivation and propagation

| Term | Definition | Aliases to avoid |
|------|-----------|-----------------|
| **Frequency** | How often the entry node is invoked per time unit (e.g., 1000 requests/min) | Request rate, traffic, load, volume |
| **Call rate** | The weight on an edge: how many times a parent node invokes the child per parent invocation (e.g., 0.8 or 3) | Edge weight, fan-out, multiplicity, call frequency |
| **Workload derivation** | The process of computing per-node usage by propagating frequency through the DAG topology | Usage calculation, cost propagation, demand derivation |
| **Derived usage** | Per-node resource consumption computed from the entry frequency and call rates through the graph | Computed usage, propagated usage, estimated usage |
| **Usage metric** | A single measurable unit of consumption on a node (e.g., compute-ms, data-out-GB, read-units) | Usage param, usage override, dimension |
| **Parameter** | A symbolic variable in the model (frequency, call rate, payload size) that can be varied for what-if analysis | Variable, input, factor |

## Inputs and outputs

| Term | Definition | Aliases to avoid |
|------|-----------|-----------------|
| **Resource representation** | The intermediate representation of what infrastructure exists, produced by parsing .tf files, Pulumi exports, or CDK synth | Infrastructure representation, infrastructure-as-code output, resource definition |
| **Cost model representation** | The JSON Schema document describing the directed acyclic graph, call rates, and per-node usage metrics | Usage model, cost model, workflow definition |
| **Cost engine** | The Python library that performs workload derivation, cost aggregation, and sensitivity analysis | Calculator, estimator, computation layer |
| **Surface** | A user-facing interface for declaring a cost model (YAML, TypeScript SDK, or Python SDK) | Interface, API, front-end, binding |
| **Flat override** | A per-resource usage value specified directly without directed acyclic graph propagation; exists for migration and edge cases | Manual override, usage override, direct usage, static usage |
| **Code generation** | The process of producing typed SDK classes from .tf files or Pulumi exports so that resource addresses and usage metrics are compile-time checked | Codegen, type generation |

## Analysis

| Term | Definition | Aliases to avoid |
|------|-----------|-----------------|
| **What-if analysis** | Varying one or more parameters to observe cost impact without changing the DAG structure | Scenario analysis, sensitivity run, parameter sweep |
| **Sensitivity analysis** | Identifying which parameters have the greatest effect on total cost | Cost sensitivity, parameter importance, critical path |
| **Node type** | A category determining which usage metrics a node accepts and whether it can have outgoing edges (e.g., compute, storage, routing) | Resource category, resource type, kind |

## Relationships

- A **Flow** starts at exactly one **Entry node** and traverses **Edges** weighted by **Call rates**
- **Workload derivation** produces **Derived usage** for each **Node** by multiplying **Entry node frequency** by the product of **Call rates** along each path
- **Resource representation** and **Cost model representation** are separate inputs to the cost engine: `resource representation × cost model representation → derived usage × pricing = cost`
- A **Node** belongs to exactly one **Node type**, which determines its valid **Usage metrics**
- A **Leaf node** has no outgoing **Edges**; a **Compute node** or **Routing node** may have outgoing **Edges**
- The **Cost engine** is implemented in **Python**; the **TypeScript surface** may reimplement the same core logic for browser or IDE use, but both must pass the same test fixtures

## Flagged ambiguities

- **"Usage"** is used three ways in this conversation: (1) **Usage metric** — a measurable unit like compute-ms, (2) **Derived usage** — the computed per-node consumption, (3) **Flat override** — a manually specified per-resource value. We reserve "usage" unqualified to mean **derived usage**; the others are always qualified.
- **"Cost model"** was used interchangeably with "usage model" in early discussion. We adopt **cost model representation** for the full DAG definition (topology + frequency + metrics), and **derived usage** for the computed per-node consumption. "Usage model" is retired.
- **"Action"** (from pulumi-cost) and **"ResourceSlot"** (from Infracost) are both replaced by **Node**. An Action is a pulumi-cost implementation detail; a ResourceSlot is an Infracost implementation detail. The domain concept is a Node in a DAG.
- **"Frequency"** in pulumi-cost meant the entry invocation rate; "call rate" was the per-edge weight. We disambiguate: **Frequency** is the entry invocation rate; **Call rate** is the per-edge weight. Never use "frequency" for per-edge weights.
- **"Propagation"** was used for both top-down (traffic flows down) and bottom-up (costs aggregate up). We reserve **Workload derivation** for the top-down computation of per-node usage and **Cost aggregation** for the bottom-up summing of costs. Never use "propagation" unqualified.

## Example dialogue

> **Dev:** "If I double the **frequency** on the **entry node**, does the **derived usage** on every **leaf node** also double?"
>
> **Domain expert:** "Only if every **call rate** is 1. If an edge has a **call rate** of 3 — meaning the parent invokes the child three times per invocation — then the **leaf node's derived usage** scales by the product of all **call rates** along the path, times the **frequency**."

> **Dev:** "So where does the **resource representation** come in? I have my **cost model representation** with the DAG and **call rates** — what does the **resource representation** add?"
>
> **Domain expert:** "The **cost model representation** tells you *how much* each **node** is used. The **resource representation** tells you *what* each node is — an `aws_lambda_function` with 256MB memory in `us-east-1`. The engine joins them: **derived usage** times pricing equals cost."

> **Dev:** "Can I just specify monthly requests as a **flat override** on each Lambda instead of building the DAG?"
>
> **Domain expert:** "You can — it's an escape hatch for edge cases. But if you have three Lambdas in a **flow** and traffic doubles, you'd need to update three **flat overrides** manually. With the DAG, you change one **frequency** and **workload derivation** recalculates everything."

> **Dev:** "What about a DynamoDB table that's called by two different Lambdas?"
>
> **Domain expert:** "The table is a **leaf node** with two incoming **edges**. Its **derived usage** is the sum of both paths: frequency × call rate from Lambda A, plus frequency × call rate from Lambda B. That's **cost aggregation** — each Lambda's contribution is derived top-down, then summed at the shared **leaf node**."