---
name: issue-pipeline
description: |
  Identifies the top 5 priority issues, spawns async implementers to create or
  advance PRs, runs quality gate reviews as each finishes, merges passing PRs,
  and iterates on failures. Completes when all issues are addressed.
---

## issue-analyzer
phase: Triage
label: Identify top 5 priority issues
as: priorities
output: priorities.json
outputMode: file-only
model: ollama-cloud/glm-5.1
reads:
  - DESIGN_PRINCIPLES.md
  - UBIQUITOUS_LANGUAGE.md
outputSchema:
  type: object
  required:
    - top5
  properties:
    top5:
      type: array
      minItems: 1
      maxItems: 5
      items:
        type: object
        required:
          - issue_number
          - title
          - action
        properties:
          issue_number:
            type: integer
          title:
            type: string
          pr_number:
            type: integer
          action:
            type: string
            enum:
              - create
              - advance
          urgency:
            type: string
            enum:
              - critical
              - high
              - medium
              - low
          summary:
            type: string
    demoted:
      type: array
      items:
        type: object
        required:
          - issue_number
          - title
        properties:
          issue_number:
            type: integer
          title:
            type: string
          reason:
            type: string
    stale:
      type: array
      items:
        type: object
        required:
          - issue_number
          - title
          - reason
        properties:
          issue_number:
            type: integer
          title:
            type: string
          reason:
            type: string

Analyze all open GitHub issues in elecnix/infra-cost-model. Spawn one scout subagent per issue to assess whether the issue is still accurate, whether an existing PR addresses it, and how urgent it is. Synthesize the findings into a ranked list of exactly 5 priorities with recommendations. For each priority, note the issue number, title, whether a PR already exists (and its number), and the recommended action (create new PR or advance existing PR). Do NOT apply any labels — the impl-orchestrator will handle that. Use `structured_output` to return the analysis with: top5 (with issue numbers, PR status, action, and urgency), demoted (issues that lost priority), and stale (issues to close).

## impl-orchestrator
phase: Implementation
label: Implement, review, and merge top 5 priorities
model: ollama-cloud/glm-5.1
progress: true
output: implementation-summary.md
outputMode: file-only

Based on the following priority analysis, orchestrate async implementers to create or advance PRs for the top 5 issues. As each implementer completes, run a quality gate review on its PR. Merge PRs that pass 100% of all quality gates with green CI and no merge conflicts. Resume implementers for review feedback or merge conflicts. Close or reject issues as needed. The chain completes when all 5 issues are addressed — merged, closed, or rejected. You are authorized to merge PRs.

Priority analysis:
{outputs.priorities}