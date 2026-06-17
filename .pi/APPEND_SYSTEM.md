These are project-level policies that all agents must follow in this repository.

<policy>
Worktrees must be created in ~/Source/infra-cost-model/<worktree-name> with bare repo at ~/Source/git-root/infra-cost-model.git
No work should be done on the main branch. Use git worktrees. Never push to default branch.
Never bypass pre-commit hooks with --no-verify.
Always create Draft pull requests.
Only the impl-orchestrator agent marks PRs as ready for review, and only before merging. All other agents create draft PRs and never change PR review status.
The impl-orchestrator agent is the ONLY agent authorized to merge PRs, and only when all four quality gates return GO (zero concerns at any severity), CI is green, and there are no unresolved review threads. All other agents must not merge PRs.

Never `find /home/nicolas`; it is too large. You must specify a subdirectory.
</policy>

These are project-level workflow conventions that all agents must follow:

<workflow>
1. Read the task description and retrieve any relevant context before starting work.
2. Rename the pi coding agent session to reflect the task, ticket number (if provided), repository name, and feature name.
3. Create or reuse a git branch after fetching from origin main.
4. Check it out into a new git worktree that is a sibling of the main directory.
5. Create a pull request draft.
6. Monitor the pull request using the GitHub monitoring tool.
7. Address all review comments by replying and resolving EACH individual thread once addressed. Do not reply into top-level comments.
8. Silently fix any CI issues. Do not post comments about CI status.
</workflow>

<conventions>
- Proceed test-first: run all tests, linting, and type checks before drafting the pull request.
- Analysis and orchestration run on the main worktree (~/Source/infra-cost-model/main) on the main branch. Pull origin main before every major step to see the latest state.
- Implementation runs in an isolated worktree (~/Source/infra-cost-model/<branch-name>). Never modify source files in the main worktree.
- When an agent needs to check out a branch to implement a feature, it must do its work in a worktree. When an agent is only analyzing or reviewing, it should run on the main worktree.
- Multiple implementers may work concurrently. Each must have its own isolated worktree so they don't conflict with each other or with main.
- All communication between agents stays internal. Do not post quality gate findings, reviewer concerns, or blocker details as GitHub comments. Only post comments when closing issues (e.g., "Fixed by #<PR_NUMBER>").
- Chain output files (priorities.json, implementation-summary.md, etc.) are written to the chain's temporary directory, not the code repository. They are ephemeral artifacts for inter-agent communication and are cleaned up automatically. Never write agent output files to the repository.
- Review-only agents must not modify source files. If review-only/no-edit instructions conflict with progress-writing instructions, review-only/no-edit wins.
- Implementers should self-review their own diffs for slop patterns (restating-code comments, dead helpers, verbose variable names, generated-sounding docstrings) before creating PRs.
</conventions>