---
name: unattended-orchestration
description: Orchestrating unattended work via the plangraph spawner and keeper.
---

# Unattended Orchestration

## When to use

Use this skill when work should run without a human in the loop: multi-step or
multi-track jobs that can be planned as cards, spawned, tracked, and gated through
the keeper. It fits a run that needs several levels of supervision and leaves
evidence behind.

Do not use it for a single interactive edit, for work that needs a live operator
decision at every step, or where no card can state an outcome and a mechanical
check. Every level is a spawned run; nothing carries over between spawns.

## Levels

A session's *name* is a project label, not proof of its level; the launch prompt
states the level. A prompt that names no level asks before loading that level's
rules. Depth and model tier are independent axes.

| Level | Job | Relaunches | Asks the operator |
| --- | --- | --- | --- |
| **L0** router | the operator's own session: routes intent, tracks PAUSE/resume, and is the only path to the operator; never executes project work, cleanups, or setup — hands them to the L1 coordinator | L1, when L1's status timestamp stays quiet >25 min from its handoff (R-coord-08) | is the operator |
| **L1** coordinator | one per run: launches L2s, merges lanes into main, pushes, cleans up | L2, past its context cap (R-coord-06); or a busy L2 whose status timestamp stays quiet >25 min from its handoff (R-coord-08) | never directly — appends `question: … \| options: …` to L0's inbox |
| **L2** orchestrator | one per track/plan: owns a worktree + branch, spawns and reviews L3 | L3, never resuming a no-change stop (R-orch-06) | never directly — same channel, via L1 |
| **L3** worker / reviewer | one closed task with an explicit return contract (docs/agent-protocol.md) | nothing (R-worker-06) | never |

Relaunch is a watch: each level reads the status timestamp the level below rewrites
and relaunches — never resumes — a busy child quiet >25 min from its handoff
(R-coord-08, the beat).

## Fleet rules in force

Binding on every level. Each line names its source; read the source, never a copy.

1. Plan graph is the source of tasks. Keeper verbs: ready, show, claim (lease), heartbeat (epoch), evidence {kind, ref}, submit, release, delegate; router-only: verdict, waive_second_family, withdraw_review, set_kind.
2. Evidence kinds: commit+ci (commit on main with Plan-Item trailer; newest CI run per check suite + name green), run-record, semaphore-task, decision. Done = merged to main + evidence attached + reviews passed; a claim is not evidence.
3. Defining items: done_criteria max 600 chars, overflow goes in the description [D-1424]; a lone lane needs a path to a goal (unblocks/goal link) or define refuses `unreachable_goal`.
4. Planning method: skills/plangraph-planning/SKILL.md (layers, task contract, report schemas).
5. L2 launch contract: L1 asks routing `"L2-LAUNCH <name> <brief path> <cwd>"`; the router launches Sonnet at effort medium with slim MCP; L2 never merges or pushes to main, runs no crons, keeps a status card <=40 lines, and sends `"RECYCLE-READY <card path>"` before 270k context tokens.
6. Quiet fleet: messages only for BLOCKED/decision, DONE or MERGE-READY with sha, GO requests, incidents, launch/recycle; no acks, no ALIVE; no recurring crons; wait with one background command that exits [D-1364, D-1377; build-L1 beat exception D-1436].
7. L1 and L2 sessions run at effort medium [D-1390].
8. CI runs only on the workstation or ci.vm; no GitHub billing [D-1436].
9. Judge reviews only HIGH risk: secrets/auth/access, keeper and plan-graph engine or live keeper changes, live deploys/migrations (D-825 GO steps), data-mutating live steps. Seat selection follows D-2488/D-2654 via `seats_for`.
10. A harness or classifier refusal is never worked around or relayed to another agent: stop and send the router the verbatim refusal, which the router turns into an OS-NN line the operator types in that session [routing DECISIONS 2.7].
11. A named model runs only over its own path, never a substitute model or route [D-1504]. qwen3.8-flash = qodercli ACP only; gemini-3.8-flash = antigravity (agy) ACP or Google AI Studio (both rate-limited: back off or send BLOCKED, never swap); muse-spark-1.3 = opencode ACP.

## How work runs

Spawn by card through the plangraph spawner:

```bash
python -m plangraph.spawner.cli spawn --role <role> --card '<json>' <prompt>
```

The other verbs are `route` (cheap first pass), `ps`, `wait`, `result`, `status`,
and `cancel`. The keeper owns lane state: `heartbeat` keeps a claimed lane alive
and `submit` closes it with evidence. A card is the unit of work; its `write_set`,
`check_cmd`, and prompt travel with the spawn.

Launch in this order: read the card, check its write set is disjoint, spawn the
role, then watch its status and result. Relaunch a quiet or capped child; never
resume it. Accept a lane only when its check passes and its report names the
evidence.

## Rules

- Delete-class rules are gone. Do not reintroduce them in prompts or review.
- Code-class rules live in tests named in docs/skills/rule-disposition.md.
- Card-class rules live in the role cards:
  [planner](cards/planner.md), [orchestrator](cards/orchestrator.md),
  [worker](cards/worker.md), [reviewer](cards/reviewer.md).

To add or change a rule, update its row in docs/skills/rule-disposition.md first,
then the named test or role card. A rule with no disposition is not in force.
