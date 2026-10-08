---
name: plangraph-planning
description: Use when planning and executing multi-step work with layered agents (project, phase, task). Defines the three layers, which model/effort each runs at, how planning and work phases spawn research, implementer and reviewer agents, the one-agent-per-task rule, the task-size contract, failure handling and report schemas, all mirrored as items in the plangraph plan graph.
---

# plangraph-planning

A planning-and-delivery method for work that is too big for one agent. Every unit of work is an
**item in the plan graph** (project, phase, task), so the human sees the same hierarchy the
agents run. Companion skills: `coding-principles` (how code is written), `unattended-orchestration`
(how layers are launched, relaunched and watched), `swarm-orchestration` (roles, wave caps, model
tiers). **This skill does not restate their numbers**: thresholds, tiers and caps live in
`swarm-orchestration`'s config; this file only says how the layers fit together.

## 1. The hierarchy (expandable and collapsible in the page)

```
Layer 1  PROJECT        one agent, Opus 5.5, medium effort
  |                     owns the intent/description; orchestrates phases; talks to the operator
  +- Layer 2  PLANNING phase (0..n, at times the operator chooses), Opus 5.5, high effort
  |    Layer 3 agents:  1. research x3-10   (Sonnet 5.5, mixed model families, in parallel)
  |                     2. synthesis        (planner merges research into one plan)
  |                     3. plan review x3    (different lenses, at least one other family)
  |                     4. refinement        (planner revises; no new agents)
  +- Layer 2  WORK phase (0..n, one per slice of an approved plan), Sonnet 5.5, high effort
       Layer 3 agents:  1. detail research x0-2 (optional, subagent-driven, fetch specifics)
                        2. refinement           (phase agent tightens task contracts)
                        3. implementers         (cheap but capable; independent tasks in parallel)
                        4. reviewers x1-3       (very cheap; other family than the implementer)
                        5. fix-tasks            (a new implementer per failure, section 5)
```

Mapping to `unattended-orchestration` levels: Layer 1 is the coordinator/orchestrator role for the
project, Layer 2 is the L2 orchestrator, Layer 3 is the L3 worker/reviewer. Layer 3 agents never
spawn (R-worker-06). Model names are *tier aliases* (`model_routing`), not pinned ids.

Two plan-graph item kinds per layer: a **phase item** (kind `planning` or `work`) with child
**task items**; both carry `layer`, `role`, `agent_id` and `state`.

## 2. One agent, one task

- Every task item is owned by **exactly one agent** for its whole life. No second agent edits that
  task's files; no agent works on two tasks at once.
- Planned file scopes must be **disjoint**. If two tasks would touch the same file, merge them
  into one task (consolidation, not locking).
- Implementers work in their own worktree/branch; the phase agent merges. No repo-wide git
  operations while agents run (`git reset --hard`, `clean`, `stash`, `gc`, `worktree prune`).
- The spawner provides the toolchain in the brief: lint, test and format commands, the coding
  rules from `coding-principles`, and the files in scope. Agents do not discover them.

## 3. Task-size contract (decided 2026-10-08)

A task is **valid** only if all of these hold; the phase agent splits or merges until they do:

| Check | Rule |
|---|---|
| Scope | at most ~3 files (plus their tests), named in the brief |
| Objective | one sentence, one outcome |
| Acceptance | one command (or short fixed list) whose pass/fail is mechanical |
| Discovery | the implementer needs no architecture decisions; the contract names interfaces |
| Size | roughly 10-40 minutes of agent work, fits a fresh context with room to spare |
| Independence | no hidden ordering; real dependencies are edges in the graph |

Too small (a one-line change, a rename): fold into a neighbouring task with the same scope.
Too big (touches many modules, "and then also..."): split on module, interface or test boundary.
See `references/task-contract.md` for the brief template.

## 4. Phases in detail

**Planning phase (Layer 2, high effort).**
1. Pose the questions; spawn 3-10 research agents (default 3-5; max 10 by R-orch-30), varying
   model family and prompt angle so findings diverge. Each returns a schema'd report, not a
   transcript.
2. Merge into one plan with phases, tasks, edges and risks.
3. Three reviewers with different lenses (correctness/feasibility, risk/security,
   simplicity/cost), at least one from a different model family than the planner.
4. Refine; record what was accepted and rejected. Output: an approved plan in the plan graph.

**Work phase (Layer 2, high effort).**
1. Optional: 1-2 detail researchers for facts the plan lacks.
2. Refine task contracts against section 3.
3. Spawn implementers: independent tasks in parallel, dependent ones in order.
4. Spawn 1-3 reviewers per task or batch, very cheap models, **a different family from the
   implementer**.
5. Any failure creates a fix-task (section 5). The phase ends when every task is done and
   reviewed; the phase agent sends a digest upward (section 6).

**Project (Layer 1, medium effort).** Holds the intent, decides *when* a planning phase is needed
(operator-driven), reads only digests, never raw agent output, and asks the operator through the
single operator channel.

## 5. Failure handling

- An implementer or reviewer failure **creates a new fix-task**, linked to the failed one by an
  edge `fixes -> <task>`; a new agent with fresh context gets the failure summary only.
- Maximum **2 fix-tasks** per original task. After the second failure the phase agent re-plans
  the task (split/merge/rewrite the contract); if that fails it escalates to Layer 1.
- Every failure is a visible item, so repeated failures point at the pipeline (bad contract, bad
  tooling, wrong model tier) and not at "an unlucky agent". Count them per phase and report them.
- Terminal/tool errors (build, lint, test, environment) are recorded as an **error record**
  attached to the task (section 6), never pasted into the done summary.

## 6. Reporting (schema'd; no transcripts upward)

Four artifacts, all JSON, defined in `references/schemas.md`:

1. **done report**: task agent to phase agent; at most 150 words, files changed, commands run
   and results, open risks.
2. **error record**: command, exit code, last 20 lines, suspected cause. Attached to the task
   item; read on demand.
3. **review findings**: reviewer to phase agent; verdict and severity-ranked findings.
4. **phase digest**: phase agent to project agent; at most 200 words: outcome, tasks done/failed,
   fix-task count, decisions needed.

Each layer reads only the artifacts from the layer directly below it.

## 7. Model-family diversity

Native Claude Code subagents are all Claude, so they give lens and tier diversity but not family
diversity. For genuine cross-family research and review use `herdr-orchestration` or the AutoOS
fleet routes; where only Claude is available, state that in the review record rather than
claiming cross-family coverage.

## 8. Checklist

- [ ] Intent written at Layer 1; planning phase(s) scheduled by the operator
- [ ] Research 3-10, mixed families; plan reviewed by 3 lenses; refinement recorded
- [ ] Every task passes the section 3 contract; file scopes disjoint; toolchain in each brief
- [ ] One agent per task; implementers parallel only when independent
- [ ] Reviewers cheap and cross-family; failures become fix-tasks (max 2) then escalate
- [ ] Done reports, error records and the phase digest filed; plan-graph states updated
