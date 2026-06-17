# Agents

This repository specifies a DAG-based infrastructure cost model. See [DESIGN_PRINCIPLES.md](./DESIGN_PRINCIPLES.md) for the foundational concepts and constraints that guide all work here.

## What to work on

- Define the Cost Model IR JSON Schema (the single source of truth for YAML, TS, and Python)
- Design the codegen pipeline (`.tf` / Pulumi / CDK → typed SDKs)
- Add concrete examples: a web API with a database backend, a serverless workflow, an LLM-augmented pipeline
- Evaluate the model against real pricing data from AWS, GCP, Azure

## What not to do

- Don't add boilerplate, CI configs, or scaffolding beyond what's needed
- Don't conflate the usage derivation layer with the pricing layer — they are separate (Principle 6)
- Don't treat flat per-resource overrides as the primary interface — DAG is the default, flat is the escape hatch (Principle 9)
- Don't write the SDKs by hand — generate types from IaC schemas (Principle 10)
- Don't design three independent interfaces — YAML, TS, and Python must share one schema and one mental model (Principle 11)