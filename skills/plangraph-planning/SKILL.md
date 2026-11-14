---
name: plangraph-planning
description: Use when planning and executing multi-step work with layered agents (project, phase, task). Defines the three layers, which model/effort each runs at, how planning and work phases spawn research, implementer and reviewer agents, the one-agent-per-task rule, the task-size contract, failure handling and report schemas, all mirrored as items in the plangraph plan graph; emits execution cards (readiness-gated, routed) for every task in that batch.
---

# plangraph-planning

For spawned writers/seats, use `plangraph-spawn wait RUN_ID --timeout SECONDS`.
Never use `pgrep -f` wait loops; completion is determined by the durable run record.

A planning-and-delivery method for work too big for one agent. Every unit of work is an **item in
the plan graph** (project, phase, task) and every task item carries an execution **card** the
keeper gates and routes, so the human sees the same hierarchy the agents run. Companion skills:
`coding-principles` (how code is written), `unattended-orchestration` (how layers are launched,
relaunched and watched). **This skill does
not restate numbers**: model tiers and review counts live in the registry policy block, wave caps in `docs/PLANNER-KIT.md`;
gate codes, leg order and the context pack live in `docs/PLANNER-KIT.md`; this file only says how
the layers fit together.

## 1. The hierarchy (expandable and collapsible in the page)

```
Layer 1  PROJECT        one agent; the route table decides
  |                     owns the intent/description; orchestrates phases; talks to the operator
  +- Layer 2  PLANNING phase (at times the operator chooses); the route table decides
  |    Layer 3 agents:  1. research          (the route table decides; different providers)
  |                     2. synthesis        (planner merges research into one plan)
  |                     3. plan review       (the route table decides; independent family)
  |                     4. refinement        (planner revises; no new agents)
  +- Layer 2  WORK phase (one per slice of an approved plan); the route table decides
       Layer 3 agents:  1. detail research      (optional; the route table decides)
                        2. refinement           (phase agent tightens task contracts)
                        3. implementers         (cheap but capable; independent tasks in parallel)
                        4. reviewers            (the route table decides; other family)
                        5. fix-tasks            (a new implementer per failure, section 8)
```

Mapping to `unattended-orchestration` levels: Layer 1 is the coordinator/orchestrator role for the
project, Layer 2 is the L2 orchestrator, Layer 3 is the L3 worker/reviewer. Layer 3 agents never
spawn (R-worker-06). The route table decides model selection, not this skill.

Two plan-graph item kinds per layer: a **phase item** (kind `planning` or `work`) with child
**task items**; both carry `layer`, `role`, `agent_id` and `state`.

Keeper definitions store kind `lane`, `gate`, `goal`, `question`, `decision`,
`operator_task` or `loop`; these stored kinds are separate from phase roles.
For a decision-only done branch, declare `evidence_kinds: [decision]` and
`review_tier: none` together. Only the router may make that declaration on create or edit. Creating or editing decision-only evidence with
`standard` or `high` is refused; set tier `none` or require implementation evidence.
A legacy item already declaring only `[decision]` can close on a verified exact
`CLOSE <key>` decision row regardless of kind or tier. Ordinary lane items without
that declaration retain the kind/tier refusal. Current-epoch evidence, origin/main
proof, claimant authorization and operator-task restrictions still apply.

## 2. One agent, one task

- Every task item is owned by **exactly one agent** for its whole life. No second agent edits that
  task's files; no agent works on two tasks at once.
- Planned file scopes are **disjoint** (enforced on cards as `write_set` overlap). If two tasks
  would touch the same file, merge them into one task (consolidation, not locking).
- Implementers work in their own worktree/branch; the phase agent merges. No repo-wide git
  operations while agents run (`git reset --hard`, `clean`, `stash`, `gc`, `worktree prune`).
- The spawner provides the toolchain in the brief: lint, test and format commands, the coding
  rules from `coding-principles`, and the files in scope. Agents do not discover them.

### Small-fix rule (D-1272, operator 2026-10-13)

L1 writes no code. An L2 orchestrator may write a fix itself only when it is **at most ~10 changed
lines** and touches **no auth, access or secrets** code; anything larger or in those areas goes
to an L3 writer. Such a fix still gets **2 seats plus the judge** like any other change, and the
lane record names `Writer: L2` (commit trailer `Writer: L2`). Every L1 and L2 brief carries this
clause (`references/task-contract.md`, "Orchestrator briefs").

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

## 4. Cards

Every task item gets a card **in the same batch** as the item that creates it; a task without a
card is not ready for work. Cards are validated by `plangraph.planner.card` (unknown fields, wrong
values and bad paths are refused, all problems at once), so write them exactly:

| Field | Content | Why |
|---|---|---|
| `outcome` | one sentence, one outcome (required) | the objective travels with the card |
| `read_set` | files read, kept minimal | feeds the context pack; smaller is cheaper |
| `write_set` | files edited, <=3 plus their tests | disjoint across tasks = one agent, one task |
| `exports` | signatures/names the task must honour | interfaces are copied, not discovered |
| `forbid_paths` | paths nobody may touch | scope stays mechanical |
| `smoke_checks` | quick commands run before the check | cheap early signal |
| `check_cmd` | one command, pass = exit 0 | mechanical acceptance, same as section 3 |
| `heldout_check` | hidden verification, never shown to the implementer | acceptance cannot be overfit |
| `window_class` | `128k` \| `256k` \| `1m` \| null | router picks the window; null = smallest that fits |
| `risk_tags` | auth, secrets, money, migration, public-contract, live-step | set explicitly: `[]` = reviewed, none |
| `estimate_tokens` | non-negative integer | honest estimate; the gate rejects missing/too big |

Card rules: **relative paths only**, no `..`, no absolute or drive paths; no secrets, hosts, home
paths or token-like strings anywhere in a card. The section 3 contract maps onto card fields:
Scope -> at most 3 entries in `write_set`, Acceptance -> one `check_cmd`.

### Batch example (loads with `plangraph.planner.service.import_batch` / `card.load_cards_yaml`)

```yaml
project: demo
cards:                      # form 1: top-level mapping, key = project:id or bare id
  api-validate:
    outcome: validate request payloads before the handler runs
    read_set: [src/api/handlers.py, src/api/schemas.py]
    write_set: [src/api/validate.py, tests/test_validate.py]
    exports: [validate_payload]
    forbid_paths: [src/store/]
    smoke_checks: [python3 -m compileall src/api]
    check_cmd: python3 -m pytest -q tests/test_validate.py
    heldout_check: python3 -m pytest -q tests/test_validate_heldout.py
    window_class: 128k
    risk_tags: []
    estimate_tokens: 40000
  store-retry:
    outcome: retry timed-out store writes once, then fail loudly
    read_set: [src/store/writes.py]
    write_set: [src/store/retry.py, tests/test_retry.py]
    check_cmd: python3 -m pytest -q tests/test_retry.py
    heldout_check: python3 -m pytest -q tests/test_retry_heldout.py
    window_class: 256k
    risk_tags: [money]
    estimate_tokens: 90000
ops:
  - op: create
    item:
      id: cli-docs                  # bare id -> project:cli-docs
      title: document the new command
      card:                         # form 2: inline on the item (shown once)
        outcome: document validate_payload in the command reference
        read_set: [docs/commands.md]
        write_set: [docs/commands.md]
        check_cmd: python3 -m pytest -q tests/test_docs.py
        heldout_check: python3 -m pytest -q tests/test_docs_heldout.py
        window_class: 128k
        risk_tags: []
        estimate_tokens: 20000
```

## 5. Phases in detail

**Planning phase (Layer 2).** Effort is medium unless the operator asks otherwise.
0. Read the plan graph first: the item text and the docs it names; ask only what the plan leaves open.
1. Ask an operator question batch covering scope, acceptance, constraints and risks.
   Give each question options, default first; do not ask again for facts already in the plan graph.
   Run one follow-up round on conflicts before research; record unresolved conflicts as blockers.
2. Spawn researchers by card via plangraph-spawn in the route table's order, from different providers.
   Use 2-3 prompt angles; each researcher returns at most 15 bullets plus sources, not a transcript.
   Put a static token cap on the card; use the registry policy block and `docs/PLANNER-KIT.md` for budgets.
   These output limits do not replace the registry's staffing or routing policy.
3. Merge into one plan with phases, tasks, edges and risks.
   Briefs say why the work exists, what it unblocks and what the last attempt did.
   State retries as facts only; use the Wording guide in `references/task-contract.md`.
4. Include one review seat of a non-Claude family different from the writer; routing stays with the keeper.
5. Refine; record what was accepted and rejected. Output: an approved plan in the plan graph,
   delivered ONLY as a plan-graph batch (`data/updates/<date>-<name>.yaml`, create/edit/priority
   ops, applied by the plangraph L1 after a router OK) carrying **tasks + cards** — every task
   item's card in that same batch (section 4). Prose plans are input; the batch is the output, and
   heartbeats take their next task from `pg ready` (DECISIONS 5.10, D-953). Cards the readiness
   gate refuses come back through the planner queue as questions (section 6) and are re-emitted
   with the next batch.

**Work phase (Layer 2).** Effort is medium unless the operator asks otherwise.
1. Optional: detail researchers for facts the plan lacks, using the research rules above.
2. Refine task contracts against section 3; keep each card in sync with its contract.
3. Spawn implementers by card via plangraph-spawn (`python3 tools/plangraph-spawn.py spawn --card @card.json --role implement ...`): independent tasks in parallel, dependent ones in order.
4. Spawn reviewers per task or batch by card via plangraph-spawn (`--role review --review-of RUN_ID`),
   using the route table; include a non-Claude family different from the writer.
5. Any failure creates a fix-task (section 8). The phase ends when every task is done and
   reviewed; the phase agent sends a digest upward (section 9).

**Project (Layer 1, medium effort).** Holds the intent, decides *when* a planning phase is needed
(operator-driven), reads only digests, never raw agent output, and asks the operator through the
single operator channel.

## 6. Readiness

The planner does **not** hand-pick models or approve its own cards. The **keeper** runs the
readiness gate over every stored card: ready items go to `pg ready`; not-ready items go to
`needs_input` with one open question (all findings listed) back through the planner queue, which
is where the planner revises the card. The planner's job is only to write cards that pass —
mechanical `check_cmd`, honest estimate, disjoint `write_set`, explicit risk tags. Gate codes,
re-run semantics and question reuse: `docs/PLANNER-KIT.md` ("Gate codes"), never copied here.

## 7. Routing

Routing is the keeper's job too: it derives the window class (the card's own, else the smallest
class holding the estimate) and an ordered D25 leg plan for each ready card; risk tags drop
training legs and set review seats, and failures rotate/retry/promote/park inside the router. The
planner only supplies an honest `window_class` and `estimate_tokens`. Leg order, retry policy and
the context pack layout: `docs/PLANNER-KIT.md`, never copied here.

## 8. Failure handling

- An implementer or reviewer failure **creates a new fix-task**, linked to the failed one by an
  edge `fixes -> <task>`; a new agent with fresh context gets the failure summary only.
- Maximum **2 fix-tasks** per original task. After the second failure the phase agent re-plans
  the task (split/merge/rewrite the contract, then a new card); if that fails it escalates to
  Layer 1.
- Every failure is a visible item, so repeated failures point at the pipeline (bad contract, bad
  tooling, wrong model tier) and not at "an unlucky agent". Count them per phase and report them.
- Terminal/tool errors (build, lint, test, environment) are recorded as an **error record**
  attached to the task (section 9), never pasted into the done summary.

## 9. Reporting (schema'd; no transcripts upward)

Four artifacts, all JSON, defined in `references/schemas.md`:

1. **done report**: task agent to phase agent; at most 150 words, files changed, commands run
   and results, open risks.
2. **error record**: command, exit code, last 20 lines, suspected cause. Attached to the task
   item; read on demand.
3. **review findings**: reviewer to phase agent; verdict and severity-ranked findings.
4. **phase digest**: phase agent to project agent; at most 200 words: outcome, tasks done/failed,
   fix-task count, decisions needed.

Each layer reads only the artifacts from the layer directly below it.

## 10. Model-family diversity

Native Claude Code subagents are all Claude, so they give lens and tier diversity but not family
diversity. For genuine cross-family research and review use `herdr-orchestration` or the AutoOS
fleet routes; where only Claude is available, state that in the review record rather than
claiming cross-family coverage.

## 11. Checklist

- [ ] Intent written at Layer 1; planning phase(s) scheduled by the operator
- [ ] Plan graph read first; question batch and conflict follow-up before routed research; refinement recorded
- [ ] Every task passes the section 3 contract; file scopes disjoint; toolchain in each brief
- [ ] Every task item has a card in the same batch; cards relative-pathed, risk tags explicit
- [ ] One agent per task; implementers parallel only when independent
- [ ] L1/L2 briefs carry the D-1272 small-fix clause; an L2 self-fix is <= ~10 lines, never auth/access/secrets, `Writer: L2`, 2 seats + judge
- [ ] Readiness and routing left to the keeper; refused cards answered from the planner queue
- [ ] Reviewers cheap and cross-family; failures become fix-tasks (max 2) then escalate
- [ ] Done reports, error records and the phase digest filed; plan-graph states updated
