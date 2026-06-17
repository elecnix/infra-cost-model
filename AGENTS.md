# Agents

This repository contains a specification for infrastructure cost modeling. See [DESIGN_PRINCIPLES.md](./DESIGN_PRINCIPLES.md) for the foundational concepts and constraints that guide all work here.

## What to work on

- Refine the specification: graph structure, node/edge schemas, propagation rules, pricing layer interface
- Add concrete examples: a web API with a database backend, a serverless workflow, an LLM-augmented pipeline
- Evaluate the model against real pricing data from AWS, GCP, Azure

## What not to do

- Don't implement anything yet — this is a spec repository
- Don't add boilerplate, CI configs, or scaffolding beyond what's needed to discuss the spec
- Don't conflate the usage derivation layer with the pricing layer; they are separate by design (see Principle 5)