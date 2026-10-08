# Layered planning hierarchy: design note (2026-10-08)

Source: operator request 2026-10-08. Companion skill: `skills/plangraph-planning/`.

## What was asked
A planning method like the "superpowers planning" skill, with three layers (project, phase, task
agents), a planning phase (research, plan, 3 reviews, refinement) and a work phase (optional detail
research, refinement, implementers, cheap cross-family reviewers, fix-task), one agent per task,
failures that spawn new tasks, tasks neither too small nor too large, and an error channel plus
compressed "done" summaries that flow upward.

## Decisions (answers to the interactive questions)
| Question | Decision |
|---|---|
| Task size | Contract-based: at most ~3 files, one objective, one acceptance command, no architecture discovery, ~10-40 min, merge same-file tasks. |
| Failure | Fix-task per failure (new agent, fresh context, failure summary only); max 2 per task, then re-plan, then escalate to Layer 1. |
| Reporting | Schema'd done report + separate error record + phase digest to the project agent. |
| Deliverable | Skill + this doc; **no keeper/page changes yet**. |

## Research on task size (no benchmarked number exists)
Sources agree on principles rather than figures: size a subtask to fit a fresh context with room to
spare; one narrow objective per worker; keep siblings independent; prefer fixed prompt chains when
subtasks are known in advance and orchestrator-workers only when they are not; route models by
task nature, not input size; keep parallel fan-out modest (heuristics seen: 3-7 partitions, about
10 concurrent subagents; neither verified against primary documentation). Hence a checklist
contract rather than a token number. Sources: Codex CLI task decomposition guide
(codex.danielvaughan.com, 2026-04-28), "Runtime-Structured Task Decomposition for Agentic Coding
Systems" (arXiv 2605.15425), Uno-Orchestra (arXiv 2605.05007), the aiwg subagent-scoping rule.
Within AutoOS the existing rules already agree: R-orch-02 (one file, exact spec), R-orch-30
(fan out at most 10, default 3-5), swarm-orchestration section 2 (scoped, verifiable,
restartable) and section 5 (overlapping file ownership means merge).

## How it fits the existing skills
- `coding-principles` supplies the implementer rules (TDD, DRY, SRP, why-comments).
- `unattended-orchestration` supplies launching, watching and relaunching layers; Layer 3 never
  spawns. Its levels are *depth* (L0-L3) and are independent of the model tier chosen per layer.
- `swarm-orchestration` supplies roles, wave caps and model aliases; this skill defers to its
  config for every number.

## Open points for the operator
1. **Plan-graph schema.** Items need `layer`, `role`, `agent_id`, a `kind` of planning/work, and a
   `fixes` edge. Not implemented; keeper and page changes are a follow-up.
2. **Cross-family reviewers** need the herdr/fleet route; with native Claude agents only lens and
   tier diversity are available (swarm-orchestration section 6).
3. The 2-fix-task cap is a starting value; revisit after the first real runs, using the
   per-phase failure counts the digest already reports.
4. The skill is stored in this repo; installing it into `AutoOS/.agents/skills/` (the canonical
   skills tree) is a separate step. Say if you want it moved.
