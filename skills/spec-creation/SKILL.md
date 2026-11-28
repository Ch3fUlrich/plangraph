---
name: spec-creation
description: Write the spec of a plan item (docs/specs/<ID>.md, .card.yaml, .tree.tsv) and run its one design review. Use when an item needs a spec before a writer can start, when a spec agent is briefed, or when a spec review comes back; not for planning the graph itself (plangraph-planning).
---

You write specs: the files a writer works from. You change only `docs/specs/` on a spec lane.
The planning rules (task contract, cards) are `skills/plangraph-planning/SKILL.md` sections 3
and 4; this skill is how a spec is made and checked.

## Before you write

1. **Reuse a reviewed spec.** Search main and every open lane first:
   `git log --all --oneline -- docs/specs/<ID>.md`. A spec that already has a review is
   merged or fixed, never written again; a brief that asks for it anyway is answered with the
   lane and sha.
2. **Read the item digest.** `pg-ctx context <key>`, until it exists `pg show <key>` plus the
   rules it names in `router/DECISIONS.md` on origin/main. The item's Why/How/done is the plan.
   Write the spec in the planning pass while the plan context is loaded, not later.
3. **Read only the code the item names**, at the base commit you record.

## The three files

- `docs/specs/<ID>.md`: header lines `plan_source`, `base commit`, `card SHA-256`,
  `tree SHA-256`, `evidence_after_deploy`; then one heading per contract field: Objective,
  Decision sources, State and ownership, Call paths, Reuse, Tests, Non-goals, Acceptance.
- `docs/specs/<ID>.card.yaml`: the card of plangraph-planning section 4, validated by
  `plangraph.planner.card`.
- `docs/specs/<ID>.tree.tsv`: one row per `write_set` path (path, write, budget, entry points,
  new or changed).

Every claim cites `path:line` or `path::Symbol` checked at the base commit. Every choice a
writer would otherwise make is written as a decision. Tests are named with the entry point
they drive and fail on the base. Keep the header digests equal to `sha256sum` of card and tree.

## Before the push

- Public scrub: `PYTHONPATH=tools/split:tools python3 tools/split/scrub_check.py --base origin/main --head HEAD .`
  must report 0 hits. It flags home paths, `user@host`, `origin/<branch>` paths, digit-mixed
  tokens and the word for secrets near a long path: reword them.
- Commit gate: `PYTHONPATH=src:tools python3 tools/ci_commit_gate.py check --base origin/main --head HEAD`;
  the Writer trailer is `<model> <run id | session id> (note)`.
- `git diff --check origin/main HEAD` is clean; push with a fast-check receipt from a work host.

## One design review, one correction round

1. Spawn the review with `--role research`, read-only in a checkout of the lane head, the leg
   pinned with `--leg` to a tools-capable leg of another family than the author.
   A `--role review` run cannot parse a design verdict.
2. The answer gives per spec `VERDICT: DESIGN-OK` or `VERDICT: DESIGN-CHANGE` and one line per
   point with a tag: SOURCE, STATE, PATH, REUSE, TEST, SCOPE; each names file or symbol, the
   defect and the change.
3. The author makes one correction round: each point taken in, or answered with a fact from the
   repository in the spec ("Review r1 <id>: rejected, <reason>").
4. Then the spec is final. There is no second review: points still open are listed in the spec
   for the implementer, who sees the code and resolves them inside the write set.
