---
name: unattended-orchestration
description: Orchestrating unattended work via the plangraph spawner and keeper.
---

Brief order: use the versioned `src/plangraph/review/{writer,review}_brief.md`
templates via `plangraph.review.prompt.render_brief`. Keep role, rules, Rust language
line, gates and commit trailer format first; put item context and task data last.
A byte-identical fixed prefix lets the prompt cache reuse it across items.

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
| **L3** worker / reviewer | one closed task with an explicit return contract (cards/worker.md, cards/reviewer.md) | nothing (R-worker-06) | via a `PG_QUESTION:` line (`rust/worker/src/question.rs:QUESTION_PREFIX`), not directly |

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
11. A named model runs only over its own path, never a substitute model or route [D-1504].
    qwen3.8-flash = qodercli ACP only; all Gemini Flash versions are retired by operator order 2026-11-14: never spawn them.
    D-5903 enables agy-gemini-4-hydrogen-high for implementation, unwitnessed.
    D-5924 supersedes D-5903's medium, inline-only review: Hydrogen reviews use agy CLI at high effort with read-only worktree access.
    Authorized quota fallbacks follow D-5952 and the Work modes section below; muse-spark-1.3 = opencode ACP.

## Work modes

The work mode decides how cards become merged code (routing DECISIONS 3.41, D-5952).
`batch` is this repository's default now. The planned UI and configuration setting offers
`auto` or a manual choice of `piecewise` or `batch`; more modes may follow.
Implementation is tracked by plangraph:WORK-MODE-S1-CONFIG, WORK-MODE-S2-AUTO,
WORK-MODE-S3-UI and WORK-MODE-S4-CONSUME under plangraph:WORK-MODE-SETTING.
`pg_worker_core::work_mode_auto::resolve` implements the `auto` policy below;
`pg-spawnd::placement::route` reports the resolved mode and deciding leg.
A quota-exhausted strong writer leg does not count as served.

| Mode | Who writes | Reviews | `auto` choice |
| --- | --- | --- | --- |
| `piecewise` | low-tier workers, one card each | one or two per card by risk | no strong writer leg is served |
| `batch` | one strong writer per brief of related cards | writer starts another-family review after fast checks, then fixes | a strong writer leg is served |

Review policy follows routing DECISIONS 3.26: currently one Hydrogen-high review per head
on non-auth lanes, with the authorized free-leg fallbacks and Sonnet high last. Credential,
auth, access and secrets lanes retain the witnessed-family condition and use witnessed
Sonnet high. This temporary policy overrides the historical per-card review counts.

In every mode:

- Every writer commit that fixes or implements keeper items carries one `Plan-Item`
  trailer per fixed item key, including subitems of a larger item (R-batch-14).
  A parent trailer or legacy `Closes:` token is insufficient.
- Before plan-order sends DONE, run `pg close-merged KEY GO-D-ID --from-open`
  without `--apply` for each item it would report DONE (R-batch-15). Report
  WOULD-CLOSE or the exact refusal reason; a refusal leaves the item open.
- Ready PRs combine into one batch PR; [R-batch-09](cards/batch.md) lists rule 3.39's exact exception tokens and conditions.
- Fast checks pass on the exact head before any review.
- Writer worktrees start at the fetched default head (R-lead-07); reviews use the exact proposed head.
- Disk admission requires at least 5 GB free on `/`; running writers stop at 2 GB (rule 7.1).
- Reviews never wait for a quota reset.
- Speedups that do not require smarter agents are the default (rule 3.41).

Batch rules, including the writer's five-round limit: [batch](cards/batch.md).
Every batch writer brief starts with `skills/batch-writer/SKILL.md` verbatim and every
review prompt of a writer head with `skills/review/SKILL.md`: an agent that is not told
the workflow in its prompt does not follow it.
These batch-specific instructions govern grouped writers and their in-session fix loop;
the per-card L3 contract above describes `piecewise` work.

Skills come first, algorithms next. The batch card's Port to code table names the keeper
items that move repetitive steps into plangraph code. Once the implementation lands and
enforces a rule, replace that rule's procedure with a pointer to the code.

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
  [worker](cards/worker.md), [reviewer](cards/reviewer.md), [batch](cards/batch.md).

To add or change a rule, update its row in docs/skills/rule-disposition.md first,
then the named test or role card. A rule with no disposition is not in force.

## Session-independent waits (D-6054)

Use [the spawner watcher protocol](../plangraph-spawn/SKILL.md) for every wait
with a pending continuation. Launch it through `plangraph watcher-launch` and
persist the job directory in the handoff. On start, including successor sessions,
read `plangraph resume-digest JOB` before spawning or scheduling follow-up work.
The detached service owns the wait and its authorized push/merge steps; result
files are the recovery contract when the orchestrator is killed.
