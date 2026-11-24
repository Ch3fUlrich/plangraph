---
name: coding-principles
description: Use at the start of any implementation, refactor, bugfix or data investigation, and before a job over a full dataset: DRY, test first, single responsibility, why-comments, changelog backtracking, verify the production path, small batches, telemetry. Not for prose or non-code tasks.
---

# Coding Principles

A compact, enforceable baseline for how agents write code in this repository and
in any repository that loads this skill. These are
**flexible** principles — adapt them to context — except TDD and backtracking,
which are **rigid**: follow them exactly.

When a stronger, more specific skill is available (e.g.
`superpowers:test-driven-development`, `superpowers:systematic-debugging`), that
skill wins. This skill is the always-on floor, not a ceiling.

Read [references/red-flags.md](references/red-flags.md) when you catch yourself
reasoning toward a shortcut on Principles 1-9: it lists the excuses and their answers.

## Principle 1 — DRY / Single Source of Truth

Every fact, rule, or piece of logic has exactly one authoritative home. Other
places reference it; they do not restate it.

- Extract a shared unit on the **second real duplication**, not the first
  (premature abstraction is its own defect) and not the third.
- Config, constants, and copy live in one place and are imported.
- In this repo specifically: a skill's `SKILL.md` is the source of truth; root
  instruction files (`CLAUDE.md`) are thin pointers, never copies of the workflow.

## Principle 2 — Test-Driven Development (rigid)

Write the failing test first, watch it fail, write the minimal code to pass,
then refactor. Defers to `superpowers:test-driven-development` when present.

- Red → Green → Refactor, one behavior at a time.
- A bug fix starts with a test that reproduces the bug.
- Never write implementation before there is a test that demands it.

## Principle 3 — Single Responsibility

Each module, file, function, and skill does one thing and has one reason to
change.

- If you can't describe a unit without "and", split it.
- File size is a smell: when a file grows past what you can hold in context,
  that usually means it took on a second responsibility.
- Hand-written source, test, script, playbook and doc files <= 400 lines.
  Over 400 -> split by responsibility before adding more.
- Split by responsibility, not by technical layer. Things that change together
  live together.

## Principle 4 — Documentation: document the *why*

Code shows what and how. Comments and docs exist for **why** — the constraint,
the tradeoff, the non-obvious reason.

- Keep `SKILL.md` / `README` in sync with behavior in the same change, never as
  a follow-up.
- Prefer a short "why" comment over a long "what" comment restating the code.
- Public interfaces get a one-line contract: what it does, how to call it, what
  it depends on.

## Principle 5 — Backtracking via changelogs and ADRs (rigid)

Any future agent must be able to reconstruct *why* a change was made and revert
it safely. See [references/changelog-backtracking.md](references/changelog-backtracking.md).

- Maintain a `CHANGELOG.md` (Keep a Changelog style) — one entry per meaningful
  change, newest first.
- Record non-obvious decisions as ADRs under `docs/decisions/NNNN-title.md`
  (context → decision → consequences).
- Commits are small and frequent, each independently revertible, with a message
  that states intent, not just mechanics.

## Principle 6 — Navigate by symbol; verify at the real thing

Use semantic tools before brute-force file reads, and verify through the tool that
observes the real thing.

- Find a symbol or its references with LSP, or `pg-ctx nav <symbol|path|key>` in
  Codex and agy, not by reading whole files (typically 80–95% fewer tokens). Use grep
  for text that is not a symbol. Serena (LSP symbols/refs/refactor) and Graphify
  (code-structure graph) do the same job where the harness provides them; see the
  `mcp-servers-setup` skill.
- A rule's source is the file or keeper item that states it, found through AGENTS.md.
  The Omnigraph graph is a projection, never the source of a rule: use the
  [structured-memory](../structured-memory/SKILL.md) skill only for what belongs in
  the graph.
- Changed something a browser renders? Drive it with Playwright before claiming it
  works — this is Principle 9 (verify through the path that actually runs) applied
  to a UI.
- If a tool is genuinely down, fall back **and say which one**. An unreported
  fallback turns a config bug into an apparent fact about the repo.

## Principle 7 — Scratch Script & Filesystem Hygiene

Never leave temporary scripts, throwaway code, or query files in random
repository locations or the root directory.

- Place all scratch work strictly inside connected scratch directories
  (`.claude/scratch/` or `scratch/`).
- Clean up or delete throwaway scripts when work is complete unless intentionally
  persisted in a designated `scratch/` folder.

## Principle 8 — Change the coupled state in the same act (rigid)

A datum is almost never in one place. Before changing anything, name what
**derives from** it and what **points at** it, and move those in the same change
or do not make the change. The failures this prevents look identical afterwards and
are invisible beforehand; examples in [references/red-flags.md](references/red-flags.md).

**Fix the condition, not the instances.** Selecting the records that have already
failed leaves the ones that merely have not run yet, and they fail identically on
the next pass. Select by the property that makes them wrong, not by the symptom
that has surfaced so far.

## Principle 9 — Verify through the path production takes (rigid)

A check that exercises the fallback proves nothing about the real system. If a
resolver tries A then B, and your test passes `None` so it uses B, then B is what
you verified — however many cases pass.

Before believing a verification, ask: **which branch does production take, and
did I take it?** Prefer asserting on the caller's real entry point over a
convenient inner function.

Corollary for output: never pipe a long-running command through `grep`/`tail`.
The output buffers, so a traceback is lost and a broken run reads as a silent
one. Redirect to a file and read the file.

## Principle 10 — Develop on small batches; run the full data once (rigid)

Iterate on the **smallest input that can show the effect** and process the full
dataset **once**, when the method has stopped changing. Climb the data ladder; never
skip a rung: (1) small artificial data with a known output, (2) a small real slice
chosen to cover the variation, (3) the full data. A red result at any rung sends you
back to rung 1. Read stored figures instead of recomputing them at scale, label every
number with its sample, share the machine.

Read [references/small-batches.md](references/small-batches.md) before you start any
job over more than a small batch of data, or any investigation of a corpus.

## Principle 11 — Telemetry: every path leaves a record that explains it (rigid)

Every command, job, unit and gate you add writes what an investigator needs to see
what happened, when, for which item and why, and something reads it: one structured
record per run, a wait that records since when and why, stored state instead of
per-read rebuilds, a refusal that names what it looked for, and a done criterion
that says how the failure shows up.

Read [references/telemetry.md](references/telemetry.md) when you add or change a
command, job, unit, gate, queue or alert.

## Checklist

At the start of any implementation, refactor, or bugfix:

- [ ] Read the source of each rule that applies; navigated by symbol, not by whole files (Principle 6).
- [ ] Wrote a failing test before implementation (Principle 2).
- [ ] Placed each new fact/logic in exactly one home (Principle 1).
- [ ] Each new unit has a single responsibility (Principle 3).
- [ ] Documented the *why* in the same change (Principle 4).
- [ ] Committed small; logged the change / ADR for backtracking (Principle 5).
- [ ] Kept repository clean; created scratch files only in connected `scratch/` folders (Principle 7).
- [ ] Named what derives from / points at the thing being changed, and moved it in the same change (Principle 8).
- [ ] Selected by the condition that makes records wrong, not by the symptom already seen (Principle 8).
- [ ] Verified through the branch production actually takes, not a convenient fallback (Principle 9).
- [ ] Iterated on synthetic data and a small, varied real batch; ran the full data at most once, at the end (Principle 10).
- [ ] Every new path writes its run record, a wait says since when and why, and stored state is not rebuilt per read (Principle 11).

## Learn as you go

- When something fails, stalls or surprises you (a wrong assumption, a tool that lied, a gate
  that blocked valid work, a check that passed wrong work), report one line to your
  orchestrator in the same turn: `LESSON <what happened> <evidence: run id, file:line, command output>`.
- The orchestrator defines an `LE-*` keeper item the same beat: what happened with evidence,
  the cause, the change in code that prevents it, a done check. A prompt line or a memory note
  is never the fix.
- Before you start work in an area, read the open and done `LE-*` items that name its files.
- A lesson that happens twice raises the priority of its item.
