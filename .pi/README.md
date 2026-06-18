# Pi Subagent Agents & Chains

This project uses [Pi subagents](https://github.com/earendil-works/pi-coding-agent) for automated workflows. Agent definitions live in `.pi/agents/` and chain definitions in `.pi/chains/`.

## Chains

### issue-pipeline

Identifies the top 5 priority issues, spawns async implementers to create or advance PRs, runs quality gate reviews as each finishes, merges PRs that pass 100%, and iterates on failures. Completes when all issues are addressed.

```
issue-analyzer  →  impl-orchestrator
  (triage)           (spawn 5 async implementers)
                          | as each completes (async notification)
                        async quality gate review
                          |
                        merge or iterate
```

**How to invoke:**

```
/run-chain issue-pipeline -- Analyze all open issues, implement the top 5, review, and merge passing ones.
```

Task description is mandatory. Example tasks:

- `Analyze all open GitHub issues, prioritize the top 5, implement PRs, review, and merge passing ones.`
- `Focus on design-principles-labeled issues; implement the top 5, review, and merge.`
- `Re-triage all issues and push forward the top 5 priority PRs.`

---

### design-principles-review-pipeline

Reviews the codebase for design principle violations, finds which ones aren't already tracked as GitHub issues, and creates issues for new violations.

```
design-principles-reviewer  →  design-principles-gap-finder  →  design-principles-issue-creator
       (review code)              (compare vs existing issues)      (create GH issues)
```

**How to invoke:**

```
/run-chain design-principles-review-pipeline -- Review the codebase for design principle violations, find gaps against existing issues, and create issues for new violations.
```

Task description is mandatory. Example tasks:

- `Review the codebase for design principle violations and create issues for any that aren't already tracked.`
- `Check the schema/ and engine/ directories for DP violations and file issues for new ones.`
- `Focus on DP#1 and DP#10 violations in the cost model module.`

---

## Agents

| Agent | Thinking | Role | Used by chains |
|-------|----------|------|----------------|
| **issue-analyzer** | high | Triages open GH issues using product owner priorities (correctness, test coverage, model completeness, real-world validation, low-hanging fruit), ranks top 5 | issue-pipeline |
| **impl-orchestrator** | high | Spawns async implementers, runs quality gates, merges passing PRs, iterates on failures | issue-pipeline |
| **pr-implementer** | high | Creates/advances PRs, test-first workflow, self-reviews for slop, fixes CI and review feedback | (spawned by impl-orchestrator) |
| **pr-quality-gate** | high | Reviews a PR through 4 parallel quality gates, returns GO/NO-GO with structured findings | (spawned by impl-orchestrator) |
| **reviewer** | high | Reviews a PR from a single angle, returns concerns with severity levels (critical/high/medium/minor) | (spawned by pr-quality-gate) |
| **design-principles-reviewer** | high | Orchestrates parallel scout subagents per DP to find violations | design-principles-review-pipeline |
| **design-principles-gap-finder** | low | Compares violations against existing GH issues to find new ones | design-principles-review-pipeline |
| **design-principles-issue-creator** | low | Creates GH issues for new DP violations | design-principles-review-pipeline |

## Invoking agents directly

You can also run individual agents with `/run`:

```
/run pr-quality-gate -- Review PR #5 for quality gate readiness
/run impl-orchestrator -- Implement the top 5 priority issues
/run pr-implementer -- Create a PR for issue #12
```

All agents reference `DESIGN_PRINCIPLES.md` and `UBIQUITOUS_LANGUAGE.md` for quality standards and canonical terminology.

## Agent Design Patterns

This project follows patterns from the [pi-subagents](https://github.com/nicobailon/pi-subagents) framework:

- **Review-only agents** (`reviewer`, `pr-quality-gate`) use `systemPromptMode: replace` to reduce prompt bloat and prevent off-task behavior. Implementation agents use `systemPromptMode: append` to inherit coding abilities.
- **Fresh context** for reviewers: `pr-quality-gate` spawns `reviewer` subagents with `context: "fresh"` so they inspect the actual diff, not inherited conversation history.
- **Three-level delegation**: orchestrator → quality gate → reviewer subagents. Each level has a clear role: orchestrator decides, quality gate synthesizes, reviewers inspect.
- **Acceptance contracts**: implementers receive explicit acceptance criteria (tests pass, lint clean, no slop, PR is draft with priority label).
- **Review loop stop rules**: stop after 3 quality gate rounds, stop if only cosmetic minor concerns remain, stop if an unapproved product/scope decision surfaces.
- **Self-review for slop**: implementers check their own diffs for AI-generated patterns (restating-code comments, dead helpers, verbose names) before creating PRs.
- **Output files go to chain temp directory**, not the code repository. They are ephemeral artifacts for inter-agent communication.

## Workflow Conventions

All agents follow these project conventions (defined in `.pi/APPEND_SYSTEM.md`):

1. **Read the task and retrieve context** before starting work.
2. **Rename the session** to reflect the task, ticket number, repo, and feature name.
3. **Create or reuse a branch** after fetching from origin main.
4. **Work in a worktree** — never on main.
5. **Create a draft PR** after all tests, lint, and type checks pass.
6. **Monitor the PR** using the GitHub monitoring tool.
7. **Address review comments** by replying and resolving each individual thread — never top-level comments.
8. **Fix CI failures without commenting** — fix the issue but do not post PR comments about CI status.

### Analysis vs Implementation

- **Analysis and orchestration** agents run on the main worktree (`~/Source/infra-cost-model/main`) and pull origin main before every major step.
- **Implementation** agents work in isolated worktrees (`~/Source/infra-cost-model/<branch-name>`).

## Merge Authorization

Only `impl-orchestrator` is authorized to merge PRs, and only when all four quality gates return GO (zero concerns at any severity), CI is green, and there are no unresolved review threads. All other agents must not merge PRs.

## Sandbox Mode (pi-less-yolo)

For safer agent execution with filesystem isolation, [pi-less-yolo](https://github.com/cjermain/pi-less-yolo) runs pi in a Docker container with restricted access.

### Setup (One-time)

```bash
git clone https://github.com/cjermain/pi-less-yolo.git ~/.config/mise/pi-less-yolo
cd ~/.config/mise/pi-less-yolo
mise run install
mise run pi:build
```

### Filesystem Isolation

When running `mise run pi` in this project:

- **Can write to:** Only the current project directory
- **Can write to:** `~/.pi/agent` (pi config, sessions, credentials)
- **Cannot write to:** Any other host directory
- **No privilege escalation:** All Linux capabilities dropped
- **No Docker access:** Container cannot access Docker socket

### Usage

```bash
# Normal sandboxed session
mise run pi

# Read-only mode (no file modifications)
mise run pi:readonly

# Non-interactive
mise run pi -- -p "summarize this repo"
```

### Configuration

| Variable | Description |
|----------|-------------|
| `PI_LOCAL_MODELS=1` | Enable localhost model servers (Ollama) |
| `PI_SSH_AGENT=1` | Enable SSH agent forwarding for git |
| `PI_CONTAINER_RUNTIME=podman` | Use Podman instead of Docker |