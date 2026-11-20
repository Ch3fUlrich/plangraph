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
alternative for a single isolated change; the unattended-orchestration skill
(Work modes) names the brief and the review of each mode. The plan lives in the keeper:
`plangraph:PHASE-BATCH-TRIAL` (goal, order, measures), one umbrella item per
checkpoint, `plangraph:PHASE-BATCH-SIZE-EXPERIMENT` (ledger and current defaults).

## Roles

- **Orchestrator** (one Claude session; Opus 5.5 medium, Fable 5.2 medium after a
  HIGH survives a fix round, the same host or tooling failure twice in a shift,
  a replan, or an operator audit): cuts packages, briefs writers, folds, runs
  CI waits and reviews, sends one GO request, merges with its own keeper token.
- **Package writer**: the first implement leg the spawner route serves, Codex first.
  While Codex is not served, Claude Sonnet or Opus write packages, in Claude Code or
  through agy; a package never waits for a quota reset.
- **Reviewer**: every writer starts the review of its own head by another model
  family and fixes the findings in its session (`skills/batch-writer/SKILL.md`,
  `skills/review/SKILL.md`); the orchestrator adds one read of the merge delta at the
  batch head ([batch card](../unattended-orchestration/cards/batch.md)).
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

Every writer brief starts with `skills/batch-writer/SKILL.md` verbatim (byte-identical
in every brief), then the items, then the run values. Where a line below differs from
that skill, the skill wins.

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
- Never wait, poll, push or fetch; the review command of the batch-writer skill is the
  one thing a writer waits for. Never set `GIT_DIR` to a real repository.

## Codex

- Before each Codex package starts, run on the selected host:
  `plangraph codex-host-readiness <worktree> workspace-write` (build/install from
  `rust/plangraph-cli`). Pass the exact sandbox mode planned for that session.
  Require exit 0 and `READY`; otherwise stop admission and resolve each printed
  point before launching. Never change sandbox mode just to pass this check.
  This Linux check uses no model tokens: version/login status, config and hooks
  metadata RPCs, sandbox startup, a commit in a disposable linked worktree on
  the selected filesystem, and at least 4 GiB MemAvailable. It never edits
  Codex settings or credentials. Folder trust must already be persisted; enabled
  hooks must be trusted/managed. Startup update checks must be disabled with
  updates centrally managed (see [Codex configuration](https://learn.chatgpt.com/docs/config-file/config-reference)).
  Unsupported Codex config/hooks RPCs fail admission; install a compatible CLI.
  A sandbox failure points to bwrap/RTM_NEWADDR restrictions; a scratch commit
  failure points to read-only Git metadata or local Git/hooks setup. The probe
  uses disposable Git metadata and does not run the target repository's hooks;
  repository-specific gates still run in the writer session.
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
   fixes or refutes it. One re-review of the delta in the reviewer's resumed session.
   Writer rounds end by the one stop rule of the batch card (R-batch-04, owned by
   `rust/pg-spawnd/src/round_progress.rs`).
4. One GO request (PR, head, CI run, review ids, umbrella key, folded keys, weak
   points); merge at that head; close the folded items and superseded lane PRs.
5. Every host pulls main, the hooks checkout and the installed binaries, and
   restarts long-running MCP servers. Merged is not live before that.
6. Append the ledger rows; every third checkpoint, set the defaults above from them.

## Waiting and handoff

When SSH exits 255, keep the existing wait deadline and retry the read-only
status probe at the waiter's normal cadence. Report the unreachable host; never
interpret a transport failure as a completed writer or replay a writer launch.
Other exit codes retain their existing failure handling. Do not increase the
deadline to conceal a failed connection.

If SSH cannot carry a brief, hand the brief file to the named host session by
message over Remote Control: include its contents (or an already accessible
attachment), destination path, base SHA, branch, gates and return contract.
A path on the orchestrator alone is insufficient. The host session writes the
file locally, acknowledges the branch/session identity and starts it once.
Read the result from the pushed branch on GitHub: fetch that exact branch and
verify the returned SHA and artifacts locally. Do not wait for SSH to recover
to read a result already pushed. The orchestrator owns this handoff; writers
retain their brief's push restrictions. If no authorized session can push,
report that blocker instead of claiming a branch result exists.

Host-local Tailscale monitoring and its deployment/peer-block test are described
in [host-link.md](../../docs/host-link.md).

The orchestrator waits with one background shell command that exits; nothing
waits inside a subagent. The handoff card carries: phase branch and head, PR,
folded branches, the running Codex session (host, herdr name, worktree, status
file), open findings, the next GO request, the ledger path.
