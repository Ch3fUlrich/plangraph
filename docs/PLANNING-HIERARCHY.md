# Layered planning hierarchy: design note (2026-10-08)

This note records **why** and **what is still open**. The method itself (layers, models, task
contract, failure handling, report schemas) is defined once, in
[`skills/plangraph-planning/`](../skills/plangraph-planning/SKILL.md) and its `references/`. Do not
copy it here; change the skill and link from here.

## Single sources of responsibility
| Topic | Owner |
|---|---|
| Layers, per-layer model/effort, phases, one-agent-per-task, task-size contract, fix-tasks, reports | [`skills/plangraph-planning/SKILL.md`](../skills/plangraph-planning/SKILL.md) |
| Brief templates | [`skills/plangraph-planning/references/task-contract.md`](../skills/plangraph-planning/references/task-contract.md) |
| Report JSON schemas | [`skills/plangraph-planning/references/schemas.md`](../skills/plangraph-planning/references/schemas.md) |
| Coding rules for implementers | AutoOS skill `coding-principles`: `AutoOS/.agents/skills/coding-principles/SKILL.md` |
| Launching, watching, relaunching layers (levels L0-L3, rules R-orch-*, R-worker-*) | AutoOS skill `unattended-orchestration`: `AutoOS/.agents/skills/unattended-orchestration/SKILL.md` |
| Roles, wave caps, model tier aliases, review diversity | AutoOS skill `swarm-orchestration`: `AutoOS/.agents/skills/swarm-orchestration/SKILL.md` (numbers in its `agent_orchestration.config.yaml`) |
| Cross-family reviewers | AutoOS skill `herdr-orchestration`: `AutoOS/.agents/skills/herdr-orchestration/SKILL.md` |

## Why a checklist contract for task size
No benchmarked task size exists. Sources agree on principles rather than figures: fit a fresh
context with room to spare, one narrow objective per worker, independent siblings, fixed prompt
chains when subtasks are known and orchestrator-workers only when they are not, route models by
task nature rather than input size, keep fan-out modest. Hence the skill uses a checklist, not a
token number. Sources: Codex CLI task decomposition guide (codex.danielvaughan.com, 2026-04-28),
"Runtime-Structured Task Decomposition for Agentic Coding Systems" (arXiv 2605.15425),
Uno-Orchestra (arXiv 2605.05007), the aiwg subagent-scoping rule. Existing AutoOS rules agree:
R-orch-02, R-orch-30, swarm-orchestration sections 2 and 5.

## Decisions taken with the operator (2026-10-08)
Task-size rule: contract-based. Failure policy: fix-task, capped, then escalate. Reporting:
schema'd done report plus separate error record plus phase digest. Scope: skill and note first,
no keeper or page changes. The resulting values are in the skill, not repeated here.

## Open points
1. **Plan-graph schema.** Items need `layer`, `role`, `agent_id`, a planning/work kind and a
   `fixes` edge. The page already derives a `role` (planning, implementation, review) per item for
   the Projects tab (`src/plangraph/web/page_model.js`, `roleOf`); an explicit `role` field on the
   item wins once the keeper stores one.
2. **Cross-family reviewers** need the herdr/fleet route; native Claude agents give lens and tier
   diversity only.
3. The fix-task cap is a starting value; revisit after the first real runs using the per-phase
   failure counts the phase digest reports.
4. The skill lives in this repo; whether to install it into `AutoOS/.agents/skills/` (the canonical
   tree) is the operator's call.
