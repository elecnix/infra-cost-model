# Agents

This repository specifies a directed-acyclic-graph-based infrastructure cost model. See [DESIGN_PRINCIPLES.md](./DESIGN_PRINCIPLES.md) for the foundational concepts and constraints. See [UBIQUITOUS_LANGUAGE.md](./UBIQUITOUS_LANGUAGE.md) for the canonical glossary — use those terms consistently in all code, docs, and discussion.

## What to work on

- Define the cost model representation JSON Schema (the single source of truth for YAML, TypeScript, and Python)
- Implement the cost engine in Python (directed acyclic graph walk, workload derivation, pricing, sensitivity analysis)
- Design the code generation pipeline (.tf / Pulumi / infrastructure-as-code tools → typed SDKs)
- Add concrete examples: a web API with a database backend, a serverless workflow, a large language model-augmented pipeline
- Evaluate the model against real pricing data from AWS, GCP, Azure

## What not to do

- Don't add boilerplate, configuration, or scaffolding beyond what's needed
- Don't conflate the usage derivation layer with the pricing layer — they are separate (Principle 6)
- Don't treat flat per-resource overrides as the primary interface — directed acyclic graph is the default, flat is the escape hatch (Principle 9)
- Don't write the SDKs by hand — generate types from infrastructure-as-code schemas (Principle 10)
- Don't design three independent interfaces — YAML, TypeScript, and Python must share one schema and one mental model (Principle 11)
- Don't implement the cost engine in TypeScript or Rust — Python is the canonical implementation (Principle 12). A TypeScript reimplementation is acceptable only for browser or IDE use, and must pass the same test fixtures.