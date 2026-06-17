---
name: design-principles-review-pipeline
description: |
  Three-step pipeline: review code for design principle violations, compare
  findings against existing GitHub issues to find gaps, then create issues for
  any new violations.
---

## design-principles-reviewer
phase: Review
label: Find DP violations
as: violations
output: violations.md
outputMode: file-only
model: ollama-cloud/glm-5.1

Review the current codebase for design principles violations. Read DESIGN_PRINCIPLES.md and UBIQUITOUS_LANGUAGE.md first, then systematically check every design principle against the code. Report each violation with its principle number, file/line, evidence, and remediation.

## design-principles-gap-finder
phase: Analysis
label: Compare violations with existing issues
as: gaps
output: gaps.md
outputMode: file-only
model: ollama-cloud/glm-5.1

Compare the following violations against existing GitHub issues in elecnix/infra-cost-model. Determine which are already tracked and which are new gaps with no existing issue. Search both open and closed issues using the gh CLI.

Violations to check:
{outputs.violations}

## design-principles-issue-creator
phase: Action
label: Create issues for new violations
model: ollama-cloud/glm-5.1

Based on the following gap analysis, create one GitHub issue per new violation in the elecnix/infra-cost-model repo. Use the design-principles label. Only create issues for violations that have no existing tracking issue.

Gap report:
{outputs.gaps}