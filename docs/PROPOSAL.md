# Plan graph — proposal v2 (M1) · plangraph-L1-main · 2026-10-01 · Sonnet 5.5

**Ask:** choose the architecture (§2). **Recommendation: C — a small "plan-keeper" service we write on SQLite, reusing
Python graphlib and Cytoscape.js; Beads' ideas, not its store.** C departs from [R2-imm], which prefers Postgres for
prevention, and from [R2-orch], which suggests Beads + a guard (that is option B); reasons in §2. Inputs: round-1 + all three round-2 files in
`routing/research/` ([R1], [R2-orch], [R2-imm], [R2-sweep]). v2 folds in the sweep and two review passes (§8).

## 1. Options (one plain sentence each)
| | Option | What it is |
|---|---|---|
| A | **Beads as-is** (`bd`, MIT, Dolt DB) | A task tracker built for AI agents: knows what is ready, blocked or parallel; has claims, MCP and a graph export. |
| B | **Beads behind a keeper** | Same, but a small gatekeeper service is the only thing allowed to touch the Dolt database; agents talk to it. |
| **C** | **Plan-keeper on SQLite** | One small program of ours that owns the plan database, enforces who may change what, answers "what is ready" and feeds the page; Postgres later if wanted. |
| D | **YAML files in git + CI check** | The plan is files in git; a merge check rejects any change to a finished item. |
| E | **Omnigraph + Cedar policy** | Our graph database extended with task links; a rule file says who may write (per branch, not per field). |
| F | **Gas Town / Gas City** | A "boss agent + worker agents" runtime on top of Beads; brings its own tmux, mail, merge queue. |
| G | **Project tools** (Vikunja, Plane, OpenProject, Jira, OneDev, Forgejo…) | Kanban/Gantt trackers for humans; arrows at most, no agent-grade "ready"; OneDev (MIT, REST+MCP) and Forgejo can stop a blocked issue from closing, but have no graph and no locked done (Forgejo claim: secondary sources). |
| H | **Agent teams / workflow engines** (Claude Code teams; Airflow, Kestra, Temporal…) | A task list inside one session, or schedulers for automated jobs — not a plan of items with owner, estimate and evidence. |
| J | **Small agent-task tools** (Taskwarrior, TasksMultiServer, taskflow-pro, task-graph-mcp, Phorge) | Taskwarrior has +READY/+BLOCKED; TasksMultiServer (MIT, alpha, 0 stars) has ready, DOT/UI, REST+MCP; taskflow-pro has a graph UI (licence unspecified); Phorge has a tree-like task graph — none locks done or records evidence, and most are young or unlicensed. |
| I | **Viewers / docs-as-code** (TaskView, sphinx-needs, Doorstop, StrictDoc, desktop PERT tools) | Draw or trace requirements; none locks finished items or computes ready; PERT is desktop-only, no API. |

Sweep verdict [R2-sweep] (several tools in it are marked UNVERIFIED there): no self-hosted app found has all of network view + computed ready/blocked + locked done + API/MCP.
**`beads_viewer` (`bv`) is treated as unusable** until someone reads its licence rider (it excludes use by or for Anthropic; sweep: "my reading, not legal advice"); we copy its *ideas* only (parallel tracks,
critical path, slack). TaskView (source-available, 61-tool MCP) is a possible *viewer*, never the store.

## 2. Requirements × options (P = prevented, D = only detected, – = missing)
| Req | A | B | **C** | D | E | F | G / H / I |
|---|---|---|---|---|---|---|---|
| 1 small agent queries | ✓ `bd ready` | ✓ via keeper | ✓ | ✗ files | ✗ no path maths | ✓ Beads | J: Taskwarrior/TasksMulti ✓, rest – |
| 2 DONE immutable | **D** (reopen/update/delete/`bd sql`) | P if no agent holds Dolt creds | **P** (agent paths) + D | P at merge only | partial: agents on unprotected branches, router-only merge + own diff check [R2-imm 24]; merge hooks UNVERIFIED | D | Jira only |
| 2 attribution from auth | – self-asserted | ✓ | ✓ token→id | git author | ✓ | – | G ✓ |
| 2 DONE needs evidence | – | ✓ | ✓ checked | CI | – | – | – |
| 3 picture + phone | basic export | own page | own page | own page | own page | no DAG | I: TaskView |
| 4 multi-project | ✓ | ✓ | ✓ `project:ID` | ✓ | ✓ | ✓ | G ✓ |
| 5a L1 autonomy | ✓ | ✓ | ✓ §4 | ✗ PR lag | ✗ | overlaps our runtime | ✗ |
| New code / infra to own | Dolt server, churn | Dolt + keeper (est. ~700 lines) | keeper service (est. 1,000–1,500 lines), no database server | CI validator | schema ext. | tmux+Dolt+Go | server each |
Why not A/B/F: no guard on closed items, any caller may claim any `--actor` [R2-imm 1–5, 15]; Dolt has no row/column rules,
so in B the keeper is the only guard and Beads' CLI/MCP (its main asset) cannot be handed to agents — B reuses only the Dolt
engine and semantics. F owns the runtime [R2-orch]. We take Beads' vocabulary (ready, blocked, claim, supersede,
`external:` links [R1]) and a JSONL export (compat UNVERIFIED, tested pilot day 1, so B stays a cheap fallback).
**Why SQLite, not Postgres:** [R2-imm] rates Postgres strongest (column grants, RLS) and SQLite weaker (no users); C relies on OS
user separation instead, to avoid a database server in the pilot. If the operator prefers Postgres now, only the storage
adapter and triggers change; it is the M4 default hardening.

## 3. C: requirement → mechanism (P/D)
| Req | Mechanism | P/D |
|---|---|---|
| 1 | `ready/blocked/waves/critical path` computed from edges (never stored flags); output ≤ 10 lines | by construction (not P/D) |
| 2 immutable | sole writer + OS isolation + DB triggers (below) | **P** for agents; **D** for keeper-user/root (chain + anchor) |
| 2 attribution | token → agent; model via run record bound in the token | P; mismatch flagged D |
| 2 evidence | keeper checks commit on `main`, CI head_sha == sha | P at done; nightly re-check D |
| 3 | `pg render` page (§5), read-only, no write path from the page | P (no write path) |
| 4 | one DB, `project:ID` edges, global validator | P (validator) |
| 5a | loop (§4): claims/transitions keeper-enforced; escalation triggers computed by `pg alerts` | P transitions; D alerts |

**Threat model.** Agents are untrusted principals with *no OS access* to the DB: it lives in a 0700 directory of its own
unix user (`plankeeper`); agents on any host reach only the keeper API (tailnet/HTTPS, token). OS isolation is the primary
control; the triggers are defence in depth against keeper bugs. **Residual (stated):** root, the operator, or anyone
running as `plankeeper` can rewrite the file — detected after the fact only; Postgres with column grants + RLS is the
M4 hardening step, same schema [R2-imm 25–28].
- **Locked rows.** Triggers key off `EXISTS(done event)`, never the `items.state` cache (which is asserted equal to the
  event fold in the same transaction). `BEFORE UPDATE/DELETE` abort on a DONE item's own row, its `depends_on` edges,
  evidence and events; `BEFORE INSERT` aborts an id/edge/evidence/event for a DONE item (fires even for `INSERT OR REPLACE`,
  so it does not depend on the per-connection `recursive_triggers` pragma). Every keeper connection is opened by one function
  that sets the pragmas and an `sqlite3` authorizer denying DDL, `writable_schema`, `DROP TRIGGER`, `ALTER`. New items may
  *depend on* DONE items and `corrects → DONE-id`; the old item stays DONE and shows "corrected by". `cancelled` is terminal
  and locked like DONE. Singleton `flock` on the keeper. Re-check findings (e.g. "sha vanished") go to a separate
  `audit_findings` table + alert; they never touch the DONE item.
- **Roles and trust.** *L1* = delegator: defines and claims items of its project, requests tokens, can cancel; it **cannot**
  set DONE. *Worker* = the run that does the work; the owner for separation is the worker run, not L1. *Reviewer* = another run.
  Tokens are keeper-issued, bound to run id + role + item, TTL ≤ lease, delivered by env/fd (never argv/cards/logs), revoked on
  submit/verdict/expiry; a worker cannot mint tokens; L1 requests, within its project only. The keeper reads the autoos run
  record (read-only access) to resolve each run's role, model and family and to confirm the run exists — L1 supplies a run id,
  never the claims about it. A `done` verdict needs a reviewer run with role=review, principal ≠ L1 ≠ worker and family ≠ worker
  family; if either family is unresolved the verdict is **refused (fail closed)**. Residual: a compromised L1 plus a real but
  colluding review run could forge DONE; routing-00's `pg digest` samples DONE items for this.
- **State machine (all allowed edges).** `open→claimed` (claim) · `claimed→open` (release, or lease expiry by reaper) ·
  `claimed→in_review` (submit, evidence present) · `in_review→claimed` (`--changes`, same epoch; 2× → `open` + alert) ·
  `in_review→done` (reviewer verdict + verified evidence) · `open|claimed|in_review→cancelled` (router/L1; not from done).
  Anything else is refused and logged. Claim is atomic with a lease (2 h, heartbeat renews) and a per-claim **epoch**;
  heartbeat/evidence/submit/outbox with a stale epoch are refused. In `in_review` the lease is frozen and a 4 h review
  deadline + reaper raises an alert instead. Keeper downtime freezes all lease clocks (extended by the outage on restart).
- **Ownership.** Definitions (id, project, title, depends_on, estimate, done_criteria, `evidence_kinds`, goal) are written by
  the router (all projects, cross-project edges, repo registry, priority) or that project's L1 (own project), only while
  `open`, as events. Worker: claim, heartbeat, evidence, submit. Reviewer: verdict. System: ready, expiry, `verified`, actor, time.
  `evidence_kinds` is per item and not substitutable: `commit+ci` requires the commit on `main`, CI `head_sha` == sha, the
  commit carrying a `Plan-Item: <id>` trailer and dated after the claim; `run-record` (non-code items) requires an autoos run
  record whose card names this item id and whose state is done.
- **Validator** on every write over the whole multi-project graph: no cycle, edge targets exist, every *non-terminal* item
  reaches a goal (DONE/cancelled items are exempt, so cancelling can never freeze the system).
- **Detection + recovery.** Hash-chained events; the head hash is appended to a root-owned log (outside agent and keeper users)
  on every DONE transition and hourly, and tagged in git; an independent remote/signing key held by routing-00 is an M4
  decision. At start the keeper refuses a DB whose head is behind the latest anchor (restored/rolled-back file) and alerts.
  Hourly `.backup` to a root-owned dir; restore is operator-only. Nightly: DONE shas still on `main` → `audit_findings`.
  Assumes agents have no sudo and do not run as the operator's user — test 1b checks this on real autoos agents.
- **Store down.** Keeper writes a signed `plan.snapshot.json` (carries epochs); agents verify the signature, read it
  read-only, keep working claimed items, and queue evidence in an outbox replayed on recovery — deduped, epoch-checked, token-signed
  (a token that expired during the outage is honoured for the outage duration), so injected or stale evidence is refused.
- **Import** (plan v3) is a router-attributed batch of events from a reviewed `plan-v3.import.yaml`, not a side door.

## 4. L1 autonomy loop (`pg` CLI; same verbs over MCP)
```
1 pg ready --project P --brief      # ≤10 lines: id | title | est | unblocks N | prio
2 pg show ID                        # done criteria, deps, evidence kinds (~300 tokens)
3 pg claim ID                       # atomic, returns epoch+lease; 409 -> next line of step 1
4 pg token ID --role writer --run W # keeper issues for worker run W (spawned via autoos-agent, card=ID)
5 pg heartbeat ID                   # while W runs; pg release ID if abandoned
6 W: pg evidence ID --commit SHA --ci URL ; pg submit ID
7 pg token ID --role reviewer --run V   # V = review run, other family; keeper checks run record
8 V: pg verdict ID --pass | --changes "why"   # keeper verifies evidence, sets done
9 goto 1 ; every loop: pg alerts --project P
```
**Escalate to routing-00 only on:** lease expired twice on one item; evidence refused twice; `--changes` twice; reviewer
silent past deadline or no eligible reviewer; item blocked > 4 h by another project's item; ready set empty while goal
unreached and nothing in flight (stall); validator error; a definition change touching another project; token/identity
anomaly; keeper unreachable > 10 min. **routing-00 through the graph:** `pg ready --all --brief`, `pg assign ID --to L1`,
`pg priority ID +n` (router-only), `pg digest` (per-project counts, stalled items) — assignment by id, not long messages.

## 5. Human page (req 3)
`pg render` → one static HTML from the signed snapshot, Cytoscape.js + a layered layout (dagre or ELK — licence/maintenance
checked day 1) **vendored in the repo**. Left-to-right start → parallel branches → goal; colour = state (open, ready, claimed,
in review, done, blocked); click → details (owner, model, evidence links, corrected-by); project filter chips; critical
path highlighted; DONE collapsed; on phone widths the first tab is the ready / blocked-by / in-progress list. Read-only.

## 6. Pilot · tests · cost
**Pilot:** import plan v3 — 84 distinct lane ids (`grep -o "^| T[0-9]-[A-Z0-9-]*" T*.md | sort -u`; STATUS says "~76"),
owners from file headers, dependencies from the Order sections + phase gates (L2 writer drafts the YAML, reviewed, never
auto-trusted); legacy "done ✓" rows without a sha import as `in_review`, flagged. Views: ready, blocked-by, waves
(graphlib layers), critical path by estimate, % done. CLI + MCP + routing hook. Modules ≤ 400 lines each (db+triggers,
state machine, evidence verifier, views, CLI, MCP, import, render, page), est. 1,000–1,500 lines total (hash chain, signing, outbox, tokens, epochs, validator, MCP, import, render).
**Tests:** (1a) agent-path attacks — edit/delete/reopen/re-point a DONE item via CLI and MCP, forged token/actor, other
agent's token, replayed outbox, stale epoch → refused + logged; (1b) OS-path — real autoos agents cannot read/write the DB file, have no sudo;
(1c) as `plankeeper` via the sqlite3 CLI: `UPDATE/DELETE/INSERT`, `INSERT OR REPLACE` on DONE rows → abort; `DROP TRIGGER`,
`writable_schema`, `ALTER RENAME` via the keeper's connection → denied; the same via a raw CLI session is the stated
residual and must be *detected* (hash chain/anchor, rollback refusal); (2) 3 agents claim one item concurrently → one winner, lease expiry reaps, fenced late submit; (3) evidence:
bogus sha, sha off `main`, CI for another sha, red CI, green commit without the item trailer, run-record for another item → refused; (4) cycle / unreachable goal / cross-project cycle refused;
(5) import → export → diff identical; (6) token cost: real `pg ready --brief` vs the files an L1 reads today (the T0–T7 lane files, 38,612 B, plus README/STATUS
when needed; all 11 plan files = 54,706 B), one tokenizer on both sides (format mock today: 10 ready lines = 667 B, ≈ 58× smaller
than the lane files in bytes — a format sketch, not a measurement); (7) screenshots desktop 1280 px + phone 390 px (browser → workstation); (8) kill the keeper → read-only
fallback + outbox replay; (9) one live L1 works a real lane from the graph with no prose orders.
**Cost/time (estimates, no measured basis yet):** M2 ≈ 2–3 days, M3 ≈ 4–5 days; ≈ 0.6–1.0 M Claude tokens (L1 Sonnet +
finals), workers/reviewers on trial/free legs; no database server, but one new long-running service (keeper HTTP/MCP + `plankeeper` unix user + root-owned anchor log + tailnet endpoint; SQLite on coding.vm; browser tests on the workstation —
placement request at test 7). Maintenance: ours; dependencies = Python stdlib (sqlite3, graphlib); Cytoscape.js (believed MIT), the layout plugin and the MCP Python SDK
(licence/churn unverified in research — day 1).

## 7. Decision requested
1. **C** (default) vs B (Beads engine + keeper) — one-line impact: B adds a Dolt server and bd version churn for the same keeper.
2. SQLite pilot, Postgres decided at M4.  3. Private remote for this repo: default no until M2.

## 8. Review record
Seat 1 and seat 2 (autoos-agent, combo `t3-driver-clean`, runs `…review-proposal-seat1-bad5af` / `…seat2-c7f6c7`):
25 + 22 findings on v1; the spawner left both writer/reviewer families **unresolved**, so D-206 cross-family is *not*
verifiably met — flagged to routing-00. v1 gaps fixed here: tool sweep folded in, P/D for every req, locked-row triggers
(v1 wrongly blocked new dependents), recursive triggers, token issuance/TTL/binding, reviewer separation, epochs, review
deadline, evidence kinds, snapshot signing, import path, anchor, whole-graph validator, unsourced claims removed.

Sonnet final (subagent, model sonnet) on v2: NOT READY → this v3 edit addresses its points (denominator 58×, bv wording, sweep tools named, state-machine edges, reviewer loop, trigger/authorizer holes, validator deadlock, evidence binding, outage/rollback, misleading P labels). Re-review by routing-00.

Sources: [R1] routing/research/2026-10-01-plan-graph-tools.md · [R2-orch] …-round2-orchestrators.md · [R2-imm] …-round2-immutability.md (numbers = its source list) · [R2-sweep] …-round2-tool-sweep.md
