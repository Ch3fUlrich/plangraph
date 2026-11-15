---
name: phase-batch-orchestration
description: Orchestrating work as phase batches (rule 3.1) with long-lived package writers, one checkpoint PR and one review per checkpoint. Use instead of one agent and one PR per small task.
---

Brief order: use the versioned `src/plangraph/review/{writer,review}_brief.md`
templates via `plangraph.review.prompt.render_brief`. Keep role, rules, Rust language
line, gates and commit trailer format first; put item context and task data last.
A byte-identical fixed prefix lets the prompt cache reuse it across items.

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
   share a file. Until PHASE-BATCH-SIZE-EXPERIMENT sets measured defaults, each
   checkpoint must have **under 100,000 diff bytes and at most 25 changed files**.
   Either exceeded budget requires splitting; item and changed-line counts do
   not override this limit. See [checkpoint evidence](../../docs/checkpoints/checkpoint-size.md).
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
- A test that fails without the fix. Run targeted tests before committing, then
  `tools/preflight.py` on the committed range before pushing.
- New code is Rust with Rust tests. New Python, shell or Node source files and
  new `tools/new_code_allowlist.txt` entries require an operator D-id (rule 9.14).
  Fixes inside existing Python files remain allowed until their port.
  No file above 400 lines.
- Blocked, or failing after two attempts: revert, write one sentence, next task.
- One status line per task in an uncommitted status file; `ALL DONE` at the end.
- Never wait, poll, push or fetch. Never set `GIT_DIR` to a real repository.

## Codex

- Check the host first (`plangraph:CODEX-HOST-READINESS`): login, trust and
  update prompts answered, a commit works.
- Start: `herdr workspace create --cwd <worktree> --label <name>`, then
  `herdr agent start <name> --kind codex --pane <pane> -- -m gpt-6.1-sol -c model_reasoning_effort="high" -c approval_policy="on-request" --sandbox workspace-write`,
  then `herdr agent prompt <name> "<read PACKAGE-x.md and carry it out>"`.
  Use `--sandbox danger-full-access -c approval_policy="never"` only after
  explicit operator approval naming the host and task; record the reason and
  approval in the writer brief. A sandbox failure alone does not grant approval.
- Review: `codex exec ... --json -o <file> - < prompt`, read-only by instruction.
- One ledger row per package and per review (tokens, minutes, done, skipped,
  findings). Fleet spawner writers stay paused while the burn limiters are unmerged.

## Checkpoint

1. At package time, plan stacked checkpoints within the byte/file budget above.
   Fold and commit each checkpoint, then run from the checkout root:
   `cargo run --manifest-path rust/Cargo.toml -p pg-hook --bin checkpoint-size -- BASE HEAD`.
   BASE is the PR target (`origin/main` for the first checkpoint, the preceding
   checkpoint head for each stacked PR); HEAD is the exact committed checkpoint.
   The command reports the merge-base PR diff including binary payloads; exit 1
   refuses an oversized package and exit 2 means measurement failed. Both block
   pushing and launching seats. Uncommitted edits are outside this measurement.
   Split along package/dependency boundaries into separate stacked branches and
   PRs, each targeting its predecessor. Never split only a seat's input while
   retaining an oversized PR. Recheck after every fold, fix or target change,
   including when retargeting a stack to main. Only after the check passes:
   push and wait for CI with one shell command.
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
