# Plan graph — proposal v2 (M1) · plangraph-L1-main · 2026-10-01 · Sonnet 5.5

**Ask:** choose the architecture (§2). **Recommendation: C — a small "plan-keeper" service we write on SQLite, reusing
Python graphlib and Cytoscape.js; Beads' ideas, not its store.** Inputs: round-1 + all three round-2 files in
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
| G | **Project tools** (Vikunja, Plane, OpenProject, Jira, OneDev…) | Kanban/Gantt trackers for humans; arrows at most, no agent-grade "ready" (OneDev/Forgejo: a blocked issue can't close — nothing more). |
| H | **Agent teams / workflow engines** (Claude Code teams; Airflow, Kestra, Temporal…) | A task list inside one session, or schedulers for automated jobs — not a plan of items with owner, estimate and evidence. |
| I | **Viewers / docs-as-code** (TaskView, sphinx-needs, Doorstop, StrictDoc, desktop PERT tools) | Draw or trace requirements; none locks finished items or computes ready; PERT is desktop-only, no API. |

Sweep verdict [R2-sweep]: no self-hosted app has all of network view + computed ready/blocked + locked done + API/MCP.
**`beads_viewer` (`bv`) is excluded** (licence rider excludes Anthropic use); we copy its *ideas* only (parallel tracks,
critical path, slack). TaskView (source-available, 61-tool MCP) is a possible *viewer*, never the store.

## 2. Requirements × options (P = prevented, D = only detected, – = missing)
| Req | A | B | **C** | D | E | F | G / H / I |
|---|---|---|---|---|---|---|---|
| 1 small agent queries | ✓ `bd ready` | ✓ via keeper | ✓ | ✗ files | ✗ no path maths | ✓ Beads | – |
| 2 DONE immutable | **D** (reopen/update/delete/`bd sql`) | P if no agent holds Dolt creds | **P** + D | P at merge only | – no per-field rule | D | Jira only |
| 2 attribution from auth | – self-asserted | ✓ | ✓ token→id | git author | ✓ | – | G ✓ |
| 2 DONE needs evidence | – | ✓ | ✓ checked | CI | – | – | – |
| 3 picture + phone | basic export | own page | own page | own page | own page | no DAG | I: TaskView |
| 4 multi-project | ✓ | ✓ | ✓ `project:ID` | ✓ | ✓ | ✓ | G ✓ |
| 5a L1 autonomy | ✓ | ✓ | ✓ §4 | ✗ PR lag | ✗ | overlaps our runtime | ✗ |
| New code / infra to own | Dolt server, churn | Dolt + keeper (~700 lines) | keeper (~1,000 lines), no server | CI validator | schema ext. | tmux+Dolt+Go | server each |
Why not A/B/F: no guard on closed items, any caller may claim any `--actor` [R2-imm 1–5, 15]; Dolt has no row/column rules,
so in B the keeper is the only guard and Beads' CLI/MCP (its main asset) cannot be handed to agents — B reuses only the Dolt
engine and semantics. F owns the runtime [R2-orch]. We take Beads' vocabulary (ready, blocked, claim, supersede,
`external:` links) and a JSONL export (compat UNVERIFIED, tested pilot day 1, so B stays a cheap fallback).

## 3. C: requirement → mechanism (P/D)
| Req | Mechanism | P/D |
|---|---|---|
| 1 | `ready/blocked/waves/critical path` are computed from edges (never stored flags, so they cannot go stale); output ≤ 10 lines | P (no stale flag) |
| 2 immutable | sole writer + DB triggers (below) | **P**; hash chain + anchor D |
| 2 attribution | token → agent; model via run record bound in the token | P; mismatch flagged D |
| 2 evidence | keeper checks commit on `main`, CI head_sha == sha | P at done; nightly re-check D |
| 3 | `pg render` page (§5) | — |
| 4 | one DB, `project:ID` edges, global validator | P (validator) |
| 5a | loop + triggers (§4) | — |

**Threat model.** Agents are untrusted principals with *no OS access* to the DB: it lives in a 0700 directory of its own
unix user (`plankeeper`); agents on any host reach only the keeper API (tailnet/HTTPS, token). OS isolation is the primary
control; the triggers are defence in depth against keeper bugs. **Residual (stated):** root, the operator, or anyone
running as `plankeeper` can rewrite the file — detected after the fact only; Postgres with column grants + RLS is the
M4 hardening step, same schema [R2-imm 25–28].
- **Locked rows.** `PRAGMA recursive_triggers=ON`; `BEFORE UPDATE/DELETE` triggers abort on a DONE item's own row, its
  `depends_on` edges, its evidence and its events; `BEFORE INSERT` triggers refuse a state/edge/evidence event for a DONE
  item. New items may *depend on* DONE items and may `corrects → DONE-id`; the old item stays DONE and shows "corrected by".
  `items.state` is only a cache, asserted equal to the event fold in the same transaction. Singleton `flock` on the keeper.
- **Tokens.** Keeper-issued, bound to agent + role + item + run id, TTL ≤ lease, delivered by env/fd (never argv/cards/logs),
  revoked on submit/verdict/expiry. A worker cannot mint tokens; L1 may request tokens only within its project; a reviewer
  token is refused if principal == owner or (resolved) model family == owner's family. The model comes from the autoos run
  record resolved server-side from the bound run id; unresolved → stored `claimed`, shown unverified.
- **State machine.** Stored events: `open → claimed → in_review → done`, `cancelled` (not from done). Claim is atomic with
  lease (2 h, heartbeat renews) and a per-claim **epoch**; heartbeat/evidence/submit/outbox replay with a stale epoch are
  refused. `in_review` has a deadline (4 h) + reaper → alert; `--changes` returns to `claimed` (same epoch), 2× → `open` + alert.
- **Ownership.** Definitions (id, project, title, depends_on, estimate, done_criteria, `evidence_kinds`, goal) are written by
  the router (all projects, cross-project edges, repo registry, priority) or that project's L1 (own project), only while
  `open`, as events. Worker: claim, heartbeat, evidence, submit. Reviewer: verdict. System: ready, expiry, `verified`, actor, time.
  `evidence_kinds` is per item and not substitutable (e.g. `commit+ci`, or `run-record` for non-code items).
- **Validator** on every write over the *whole* multi-project graph: no cycle, every item reaches a goal, edge targets exist.
- **Detection layer.** Hash-chained events; head hash appended hourly to a root-owned log outside the agent and keeper users
  and tagged in git; an independent remote/signing key held by routing-00 is an M4 decision. Nightly: DONE shas still on `main`.
- **Store down.** Keeper writes a signed `plan.snapshot.json` (carries epochs); agents verify the signature, read it
  read-only, keep working claimed items, and queue evidence in an outbox replayed on recovery — deduped, epoch- and
  signature-validated, so injected or stale evidence is refused.
- **Import** (plan v3) is a router-attributed batch of events from a reviewed `plan-v3.import.yaml`, not a side door.

## 4. L1 autonomy loop (`pg` CLI; same verbs over MCP)
```
1 pg ready --project P --brief      # ≤10 lines: id | title | est | unblocks N | prio
2 pg show ID                        # done criteria, deps, evidence kinds (~300 tokens)
3 pg claim ID                       # atomic, returns epoch+lease; 409 -> next line of step 1
4 pg token ID --role writer         # keeper issues; spawn L2/L3 via autoos-agent with card=ID
5 pg heartbeat ID                   # while the worker runs
6 worker: pg evidence ID --commit SHA --ci URL --run RUN ; pg submit ID
7 reviewer (other principal+family): pg verdict ID --pass | --changes "why"   # keeper verifies, sets done
8 goto 1 ; every loop: pg alerts --project P
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
state machine, evidence verifier, views, CLI, MCP, import, render, page), est. ~1,000 lines total.
**Tests:** (1a) agent-path attacks — edit/delete/reopen/re-point a DONE item via CLI and MCP, forged token/actor, other
agent's token, replayed outbox, stale epoch → refused + logged; (1b) OS-path — agent user cannot open the DB file;
(1c) as `plankeeper`, direct `UPDATE/DELETE/INSERT` on DONE rows → trigger abort; tamper with the file → hash-chain/anchor
check fails; (2) 3 agents claim one item concurrently → one winner, lease expiry reaps, fenced late submit; (3) evidence:
bogus sha, sha off `main`, CI for another sha, red CI → refused; (4) cycle / unreachable goal / cross-project cycle refused;
(5) import → export → diff identical; (6) token cost: real `pg ready --brief` vs reading the plan files, one tokenizer on
both sides (format mock today: 10 ready lines = 667 B vs plan files 54,706 B ≈ 82× in bytes — format sketch, not a
measurement); (7) screenshots desktop 1280 px + phone 390 px (browser → workstation); (8) kill the keeper → read-only
fallback + outbox replay; (9) one live L1 works a real lane from the graph with no prose orders.
**Cost/time (estimates, no measured basis yet):** M2 ≈ 1.5–2.5 days, M3 ≈ 3–4 days; ≈ 0.6–1.0 M Claude tokens (L1 Sonnet +
finals), workers/reviewers on trial/free legs; no new server (SQLite on coding.vm; browser tests on the workstation —
placement request at test 7). Maintenance: ours; dependencies = Python stdlib (sqlite3, graphlib), Cytoscape.js (MIT),
layout plugin and MCP Python SDK (licence/churn to verify day 1).

## 7. Decision requested
1. **C** (default) vs B (Beads engine + keeper) — one-line impact: B adds a Dolt server and bd version churn for the same keeper.
2. SQLite pilot, Postgres decided at M4.  3. Private remote for this repo: default no until M2.

## 8. Review record
Seat 1 and seat 2 (autoos-agent, combo `t3-driver-clean`, runs `…review-proposal-seat1-bad5af` / `…seat2-c7f6c7`):
25 + 22 findings on v1; the spawner left both writer/reviewer families **unresolved**, so D-206 cross-family is *not*
verifiably met — flagged to routing-00. v1 gaps fixed here: tool sweep folded in, P/D for every req, locked-row triggers
(v1 wrongly blocked new dependents), recursive triggers, token issuance/TTL/binding, reviewer separation, epochs, review
deadline, evidence kinds, snapshot signing, import path, anchor, whole-graph validator, unsourced claims removed.

Sources: [R1] routing/research/2026-10-01-plan-graph-tools.md · [R2-orch] …-round2-orchestrators.md · [R2-imm] …-round2-immutability.md (numbers = its source list) · [R2-sweep] …-round2-tool-sweep.md
