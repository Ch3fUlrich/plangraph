---
name: phase-batch-orchestration
description: Orchestrating work as phase batches (rule 3.1) with long-lived package writers, one checkpoint PR and one review per checkpoint. Use instead of one agent and one PR per small task.
---

# Phase-batch orchestration

Use this when several plan items change one repository and can land together.
The per-task way (one writer, one PR, one GO row per item) is only the
alternative for a single isolated change. The plan lives in the keeper:
`plangraph:PHASE-BATCH-TRIAL` (goal, order, measures), one umbrella item per
checkpoint, `plangraph:PHASE-BATCH-SIZE-EXPERIMENT` (ledger and current defaults).

## Roles

- **Orchestrator** (one Claude session; Opus 5.5 medium, Fable 5.2 medium after a
  HIGH survives a fix round, the same host or tooling failure twice in a shift,
  a replan, or an operator audit): cuts packages, briefs writers, folds, runs
  CI waits and reviews, sends one GO request, merges with its own keeper token.
- **Package writer**: Codex `gpt-6.1-sol` high in its native CLI does the bulk;
  a Claude Sonnet subagent only small fixes.
- **Reviewer**: at least one review per checkpoint. Codex reviews Claude-written
  code; a cheap reviewer of another family or Claude Sonnet reviews Sol-written code.
- **Router**: writes the GO row, logs operator decisions.

## Packages and branches

1. Fast-forward the host's main checkout and `git fetch` before any worktree is
   cut. Work never starts from a local main; the base is `origin/main` or
   `origin/phase/<name>`.
2. Group items that share a source file into one package; two packages never
   share a file. Default size: about 10 items per package, one combined
   checkpoint of about 2000 changed lines (see the ledger for the current best).
3. `phase/<name>` is cut from `origin/main`; one draft PR to main carries CI.
   Each writer has its own worktree and cluster branch cut from the phase
   branch. Only the orchestrator folds (merge commit with the item's
   `Plan-Item` trailer) and pushes. Merge `origin/main` into the phase branch daily.
4. One umbrella keeper item per checkpoint, claimed by the orchestrator, folded
   keys listed in its text, no `depends_on` edges to them.
5. Pick the host with `plangraph:PACKAGE-HOST-BALANCER` (until it exists: the
   host with the lowest load and most free memory). Run packages in parallel
   when they share no file.

## Writer brief (fixed rules)

- One commit per task; trailers `Plan-Item:`, `Writer:`, and `Changelog: skip (...)`
  when the commit adds no fragment.
- A test that fails without the fix. Targeted tests only. `tools/preflight.py`
  on the commit range before each commit.
- New code is Rust. Fixes inside existing Python stay Python until the port.
  No file above 800 lines.
- Blocked, or failing after two attempts: revert, write one sentence, next task.
- One status line per task in an uncommitted status file; `ALL DONE` at the end.
- Never wait, poll, push or fetch. Never set `GIT_DIR` to a real repository.

## Codex

- Check the host first (`plangraph:CODEX-HOST-READINESS`): login, trust and
  update prompts answered, a commit works.
- Start: `herdr workspace create --cwd <worktree> --label <name>`, then
  `herdr agent start <name> --kind codex --pane <pane> -- -m gpt-6.1-sol -c model_reasoning_effort="high" -c approval_policy="never" --sandbox danger-full-access`,
  then `herdr agent prompt <name> "<read PACKAGE-x.md and carry it out>"`.
  The sandboxed modes failed on our hosts (no commits, or no start at all).
- Review: `codex exec ... --json -o <file> - < prompt`, read-only by instruction.
- One ledger row per package and per review (tokens, minutes, done, skipped,
  findings). Fleet spawner writers stay paused while the burn limiters are unmerged.

## Checkpoint

1. Fold, push, wait for CI with one shell command.
2. Review the whole phase diff on that one head; the prompt names each folded
   item and its intent and forbids findings on unchanged code.
3. One fix brief with every finding; the writer reproduces each with a test and
   fixes or refutes it. One re-review of the delta. Never a fourth writer round.
4. One GO request (PR, head, CI run, review ids, umbrella key, folded keys, weak
   points); merge at that head; close the folded items and superseded lane PRs.
5. Every host pulls main, the hooks checkout and the installed binaries, and
   restarts long-running MCP servers. Merged is not live before that.
6. Append the ledger rows; every third checkpoint, set the defaults above from them.

## Waiting and handoff

The orchestrator waits with one background shell command that exits; nothing
waits inside a subagent. The handoff card carries: phase branch and head, PR,
folded branches, the running Codex session (host, herdr name, worktree, status
file), open findings, the next GO request, the ledger path.
