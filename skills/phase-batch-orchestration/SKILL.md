---
name: phase-batch-orchestration
description: Orchestrating work as phase batches (rule 3.1) with long-lived package writers, checkpoint PRs and one review per checkpoint. Use instead of one agent and one PR per small task.
---

# Phase-batch orchestration

## When to use

Use this skill when several plan items change the same repository and can land
together: line fixes, a feature phase, a migration slice set. It replaces the
per-task way (spawn one writer per item, one PR, seats and a GO row per item),
which stays available only as the alternative for a single isolated change.

Keeper items that carry the plan: `plangraph:PHASE-BATCH-TRIAL` (goal, order of
work, measures) and one umbrella item per checkpoint (`plangraph:PHASE-<NAME>-<N>`).

## Roles

| Role | Who | Does |
|---|---|---|
| Orchestrator | one Claude session (Opus 5.5 medium by default) | cuts packages, briefs writers, folds branches, runs CI waits and reviews, sends one GO request per checkpoint, talks to the hosts |
| Package writer | Codex `gpt-6.1-sol` high in its native CLI (bulk work); Claude Sonnet subagent (small fixes) | works one package in one worktree, one commit per item |
| Reviewer | Codex reviews Claude-written code; mimo or Claude Sonnet reviews Sol-written code | one review per checkpoint, on one head |
| Router | routing session | writes the GO row and logs operator decisions |
| Merger | the session that holds the keeper claim on the umbrella item | runs `merge-part` at the GO head |

Move the orchestrator back to Fable 5.2 medium when one of these happens: a
checkpoint review still has a HIGH after its fix round; the same class of host
or tooling failure appears a second time in one shift; a package must be replanned;
the operator asks for an audit. Go back to Opus when the next checkpoint merges clean.

## Cutting packages

1. List the open items and the source files each one changes (from its lane
   branch, PR or description).
2. Group items that share a source file into one package. Two packages never
   share a file. A package is 5 to 10 items; a checkpoint is at most about 8 to
   12 items or 1500 changed lines, and lives at most two days.
3. Order the items inside a package so that an item never needs a later one.
4. Create one umbrella keeper item per checkpoint. List the folded item keys in
   its description and done criteria; give it no `depends_on` edges to them
   (the merger could not claim it).

## Branches

- `phase/<name>` is cut from `origin/main`. One draft PR `phase/<name>` to
  main carries CI; open it after the first fold.
- Each writer works on a cluster branch cut from the phase branch
  (`phase/<name>-<cluster>`) in its own worktree. Writers never push to the
  phase branch or to main.
- The orchestrator folds a finished cluster branch into the phase branch with a
  merge commit that carries the item's `Plan-Item` trailer, then pushes.
- Merge `origin/main` into the phase branch at least once a day.

## Writer brief

Every brief states: the worktree and branch; the file cluster the writer owns;
the ordered task list with the item key, the defect or goal, and the done check
per task; and these fixed rules:

- One commit per task. Trailers, last lines, in this order: `Plan-Item:`,
  `Writer:` (`codex/<model>-<effort> (native CLI, <D-id>)` or
  `claude-<model> <session id> (<note>)`), `Changelog: skip (<reason>)` when the
  commit adds no `changelog.d` fragment.
- A test that fails without the fix; run it once before the fix and say so.
- Targeted tests only, without xdist. Run `tools/preflight.py` for the commit
  range before each commit; it reports every policy failure at once.
- New modules are Rust. Fixes inside existing Python stay Python.
- A task that is blocked, too large or still failing after two attempts is
  reverted and skipped with one sentence; the writer goes on with the next.
- A status line per task in an uncommitted status file.
- Never wait and never poll: no CI watch, no `sleep` loop, no background
  command. A Claude subagent commits, pushes its cluster branch and ends. A
  Codex session commits only; the orchestrator fetches and pushes.
- Never run anything with `GIT_DIR` pointing at a real repository; verify
  against a throwaway repository.

## Running Codex

- Native CLI, one session per package, effort high. On a host with herdr:
  `herdr workspace create --cwd <worktree> --label <name>`, then
  `herdr agent start <name> --kind codex --pane <pane> -- -m gpt-6.1-sol -c model_reasoning_effort="high" -c approval_policy="never" --sandbox workspace-write --add-dir <main repo>/.git`,
  then `herdr agent prompt <name> "<read the package file and carry it out>"`.
  The sandbox has no network: fetch lane branches before the start.
- Reviews: `codex exec --sandbox read-only --json -o <file> - < prompt`, once per
  checkpoint.
- Keep a token ledger per run and report it at each checkpoint. One package
  session and one review at a time. Fleet spawner writers stay paused while the
  burn limiters (quota fail-fast, round cap, alarms) are not merged.

## Checkpoint

1. Fold every finished cluster branch; push the phase branch; wait for CI with
   one shell command.
2. Review on that one head: the whole phase diff, in chunks of at most 400
   changed lines when the reviewer has no tools. The prompt names each folded
   item and its intent, asks for HIGH, MED and LOW with file and line, and
   forbids findings on code the phase did not change.
3. Check each HIGH and MED against the code. One fix brief with all findings
   that hold (and the refutations of the rest); one writer per file cluster.
4. One re-review of the fix delta. The round cap applies: after three writer
   rounds on a checkpoint the item is replanned, never a fourth round.
5. One GO request to the router: phase PR, head, CI run, review run ids, the
   umbrella item key as the PLAN key, the folded keys as evidence, the weak
   points. The merger merges at that head. The claimants then close each folded
   item; lane PRs that were folded are closed as superseded.
6. After the merge: every host pulls main, the hooks checkout and the installed
   binaries; long-running MCP servers restart. Merged is not live until then.

## Waiting

The orchestrator waits with one background shell command that exits (a CI
watch, a review script, an `until` loop on a status file over ssh). It never
waits inside a subagent and never lets a subagent wait.

## Handoff

State goes to the handoff card: phase branch and head, open PR, which cluster
branches are folded, the running Codex session (host, herdr name, worktree,
status file), open review findings, the next GO request. The plan stays in the
keeper items; the method stays in this skill.
