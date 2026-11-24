---
name: coding-principles
description: Use at the start of any implementation, refactor, bugfix or data investigation, and before a job over a full dataset: DRY, test first, single responsibility, why-comments, changelog backtracking, verify the production path, small batches, telemetry. Not for prose or non-code tasks.
---

# Coding Principles

The baseline for how agents write code here and in any repository that loads this
skill. Principles marked rigid are followed exactly; the rest adapt to context.
A stronger, more specific skill (e.g. `superpowers:systematic-debugging`) wins; this is the floor.
Read [references/red-flags.md](references/red-flags.md) when you catch yourself
reasoning toward a shortcut on Principles 1-9.

## Principle 1 — DRY / Single Source of Truth

Every fact, rule, or piece of logic has exactly one authoritative home. Other
places reference it; they do not restate it.

- Extract a shared unit on the **second real duplication**, not the first and not the third.
  Config, constants, and copy live in one place and are imported. A skill's `SKILL.md` is the source of truth; root instruction files (`CLAUDE.md`) are thin pointers.

## Principle 2 — Test-Driven Development (rigid)

Write the failing test first, watch it fail, write the minimal code to pass,
then refactor. Defers to `superpowers:test-driven-development` when present.

- Red → Green → Refactor, one behavior at a time; a bug fix starts with a test that reproduces it.
  Never write implementation before a test demands it.

## Principle 3 — Single Responsibility

Each module, file, function, and skill does one thing and has one reason to change.
If you can't describe a unit without "and", split it.

- Hand-written source, test, script, playbook and doc files <= 400 lines.
  Over 400 -> split by responsibility (not by technical layer) before adding more.

## Principle 4 — Document the *why*

Comments and docs state the constraint, the tradeoff, the non-obvious reason; code shows what and how.

- Keep `SKILL.md` / `README` in sync with behavior in the same change, never as a follow-up.
  Public interfaces get a one-line contract: what it does, how to call it, what it needs.

## Principle 5 — Backtracking via changelogs and ADRs (rigid)

Any future agent must be able to reconstruct *why* a change was made and revert
it safely. See [references/changelog-backtracking.md](references/changelog-backtracking.md).

- Maintain a `CHANGELOG.md` (Keep a Changelog style), one entry per meaningful change, newest first.
- Record non-obvious decisions as ADRs under `docs/decisions/NNNN-title.md`.
- Commits are small, each independently revertible, with a message that states intent.

## Principle 6 — Navigate by symbol; verify at the real thing

Use semantic tools before brute-force file reads; verify through the tool that observes the real thing.

- Find a symbol or its references with LSP, or `pg-ctx nav <symbol|path|key>` in
  Codex and agy, not by reading whole files (typically 80–95% fewer tokens). Use grep
  for text that is not a symbol. Serena (LSP symbols/refs/refactor) does the same job
  where the harness provides it; see the `mcp-servers-setup` skill.
- Graphify (code-structure graph) answers broad code-structure questions where the harness provides it.
- A rule's source is the file or keeper item that states it, found through AGENTS.md.
  Omnigraph memory is for recall ("what did we decide"), never the source of a rule:
  use the [structured-memory](../structured-memory/SKILL.md) skill only for what belongs in
  the graph.
- Changed something a browser renders? Drive it with Playwright before claiming it works.
- If a tool is genuinely down, fall back **and say which one**.

## Principle 7 — Scratch script and filesystem hygiene

Put throwaway scripts and query files only in `.claude/scratch/` or `scratch/`, never in
the repository root or random locations; delete them when the work is done.

## Principle 8 — Change the coupled state in the same act (rigid)

Before changing anything, name what **derives from** it and what **points at** it, and
move those in the same change or do not make the change. Examples in
[references/red-flags.md](references/red-flags.md).

**Fix the condition, not the instances.** Select by the property that makes them wrong,
not by the symptom that has surfaced so far.

## Principle 9 — Verify through the path production takes (rigid)

A check that exercises the fallback proves nothing about the real system. Before
believing a verification, ask: **which branch does production take, and did I take
it?** Prefer asserting on the caller's real entry point over a convenient inner function.

Never pipe a long-running command through `grep`/`tail` (a traceback is lost); redirect to a file.

## Principle 10 — Develop on small batches; run the full data once (rigid)

Iterate on the **smallest input that can show the effect** and process the full
dataset **once**, when the method has stopped changing. Climb the data ladder; never
skip a rung: (1) small artificial data with a known output, (2) a small real slice
chosen to cover the variation, (3) the full data. A red result at any rung sends you
back to rung 1. Read stored figures instead of recomputing them at scale, label every
number with its sample, share the machine.

Read [references/small-batches.md](references/small-batches.md) before any job over a full dataset or corpus.

## Principle 11 — Telemetry: every path leaves a record that explains it (rigid)

Every command, job, unit and gate you add writes what an investigator needs to see
what happened, when, for which item and why, and something reads it: one structured
record per run, a wait that records since when and why, stored state instead of
per-read rebuilds, a refusal that names what it looked for, and a done criterion
that says how the failure shows up.

Read [references/telemetry.md](references/telemetry.md) when you add or change a
command, job, unit, gate, queue or alert.

Read [references/checklist.md](references/checklist.md) before you report work done.

## Learn as you go

- When something fails, stalls or surprises you (a wrong assumption, a tool that lied, a gate
  that blocked valid work, a check that passed wrong work), report one line to your
  orchestrator in the same turn: `LESSON <what happened> <evidence: run id, file:line, command output>`.
- The orchestrator defines an `LE-*` keeper item the same beat: what happened with evidence,
  the cause, the change in code that prevents it, a done check. A prompt line or a memory note
  is never the fix.
- Before you start work in an area, read the open and done `LE-*` items that name its files.
- A lesson that happens twice raises the priority of its item.
