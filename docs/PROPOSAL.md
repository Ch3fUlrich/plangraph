# Plan graph — proposal (M1) · plangraph-L1-main · 2026-10-01 · Sonnet 5.5

**Ask:** pick the architecture (§2). **Default recommendation: C — a small "plan-keeper" service we write, on SQLite,
with graphlib + Cytoscape reused.** Evidence: round-1/round-2 research in `routing/research/`; the round-2 *tool sweep*
is still pending, so §1 may gain rows (v2 of this file) but cannot change the immutability finding.

## 1. Options (one plain sentence each)
| | Option | What it is |
|---|---|---|
| A | **Beads as-is** (`bd`, MIT, Dolt DB) | An issue tracker built for AI agents: it knows what is ready, blocked or parallel and has claims, MCP and a graph page. |
| B | **Beads behind a keeper** | Same, but a small gatekeeper service is the only thing allowed to touch the Dolt database; agents talk to the gatekeeper. |
| **C** | **Plan-keeper on SQLite (recommended)** | One small program of ours that owns the plan database, enforces who may change what, answers "what is ready", and draws the page; Postgres later if wanted. |
| D | **YAML files in git + CI check** | The plan is files in a git repo; a merge check rejects any change to a finished item. |
| E | **Omnigraph + Cedar policy** | Our existing graph database, extended with task links and a rule file saying who may write. |
| F | **Gas Town / Gas City** | A ready-made "boss agent + worker agents" runtime built on Beads; it brings its own tmux, mail and merge queue. |
| G | **Project tools** (Vikunja, Plane, OpenProject, Jira…) | Ordinary kanban/Gantt trackers for humans; few have dependency logic, none computes "ready" for agents. |
| H | **Claude Code agent teams** | A shared task list inside one Claude session; experimental. |
| ? | **Round-2 tool sweep** (PERT planners, traceability tools, workflow engines, note apps, GitLab/OneDev) | Pending from routing-00; will be scored with the same table. |

## 2. How each meets the requirements (P = prevented by the system, D = only detected, – = missing)
| Req | A Beads | B Beads+keeper | **C keeper** | D git files | E omnigraph | F Gas Town | G PM tools | H teams |
|---|---|---|---|---|---|---|---|---|
| 1 small agent queries (ready/blocked/parallel/owner) | ✓ `bd ready --brief` | ✓ via keeper | ✓ same verbs | ✗ read files | ✗ no path maths | ✓ via Beads | – | ✓ one session |
| 2 DONE immutable | **D** (reopen/update/delete/`bd sql` all work) | P only if no agent holds a Dolt credential | **P** (DB triggers + sole writer) + D (hash chain) | P at merge only | – (no per-field rule) | D (same as Beads) | Jira only | – |
| 2 attribution from auth | – self-asserted | ✓ keeper | ✓ token → agent id; model from run record | git author (forgeable) | ✓ token | – | ✓ | – |
| 2 DONE needs evidence | – no field | ✓ keeper | ✓ keeper checks sha on main + CI | CI | – | – | – | hook |
| 3 human picture + phone | basic `--html` | own page | **own page** (Cytoscape) | own page | own page | dashboard, no DAG | Gantt arrows | terminal |
| 4 multi-project + router | ✓ | ✓ | ✓ `project:ID` edges | ✓ | ✓ | ✓ rigs | ✓ | ✗ |
| 5a L1 autonomy loop | ✓ | ✓ | ✓ §4 | ✗ PR latency | ✗ | owns runtime (overlap) | ✗ | ✗ |
| New infra / maintenance risk | Dolt server; release churn | Dolt + keeper (two parts) | **one Python process, stdlib + MCP SDK** | none, but weak locks | schema extension | tmux+Dolt+Go; "vibe coded" | server per tool; paywalls | experimental |

**Why not A/B/F:** Beads has no guard on closed items and trusts any caller's `--actor` (round 2 [1–5, 15]); Dolt has no
row/column rules, so in B the keeper is the *only* guard and Beads' CLI/MCP, its main asset, can't be given to agents —
we would carry Dolt *and* write the keeper. F additionally owns the runtime. **What we do take from Beads:** vocabulary
and semantics (ready, blocked, claim, supersede, `external:` links); a JSONL export so a later switch stays possible
(format compatibility is UNVERIFIED — checked in pilot day 1, so B remains a cheap fallback).
Nothing found does req 2 by prevention out of the box; the operator's doubt was checked: the nearest products fail 2.

## 3. Mechanism of C
- **Single writer.** Only `plankeeperd` (own unix user, DB file mode 0600) writes. Agents get a narrow CLI/MCP
  (`ready, show, claim, heartbeat, evidence, submit, verdict, alerts`) and a read-only JSON snapshot. No SQL, no files.
- **Prevented (DB level, even against the keeper's own bugs):** `BEFORE UPDATE/DELETE` triggers `RAISE(ABORT)` on any row
  whose item is `done` (item, edges into it, evidence, events); append-only `events` table; state = fold over events.
  A correction is a new item with a `corrects → DONE-id` edge; the old item stays DONE and shows "corrected by".
- **Identity.** Per-agent bearer token issued by router/L1 at spawn (token → agent id, role, project, item scope);
  actor and time are set by the keeper, never sent by the client; `model` is read from the autoos-agent run record by
  run id (else stored as `claimed`, shown unverified).
- **State machine** (stored: `open → claimed → in_review → done`; `cancelled`; *ready/blocked are computed*, never stored):
  claim is atomic with a lease TTL (default 2 h, heartbeat renews, reaper returns expired to `open`); `in_review` needs
  evidence; `done` only by a reviewer ≠ owner, and the keeper verifies evidence (commit reachable from `main` of the
  project's registered repo; CI check-run `success` where the project uses CI; or an autoos run-record id).
  Skipped states and `done → anything` are refused and logged.
- **Ownership.** Definitions (id, project, title, depends_on, estimate, done_criteria, goal, priority) = router (any
  project, cross-project edges) or that project's L1 (own project), editable only while `open`, as a new event.
  Worker = claim, heartbeat, evidence, submit. Reviewer = verdict. System = ready, lease expiry, `verified`, actor, time.
- **Validator** on every write: no cycle, every item reaches a goal node, DONE has verified evidence, edge targets exist.
- **Detected (second layer):** hash-chained events; head hash anchored hourly as a signed git tag in this repo (a writer
  with DB access can recompute a chain, so the anchor lives outside the DB); nightly re-verify that DONE shas are on main.
- **Residual risk (stated):** root/the operator can edit the file; so can anyone holding the keeper user. Postgres with
  column grants + RLS is the hardening step for adoption (M4), same schema.
- **Store down:** `plankeeperd` writes `plan.snapshot.json` after every commit; agents fall back to read-only reads of it,
  keep working on already-claimed items, and queue evidence in a local outbox replayed (re-validated) on recovery.

## 4. L1 autonomy loop (what an L1 runs alone; `pg` = CLI, same verbs over MCP)
```
1  pg ready --project P --brief        # ≤ 10 lines: id | title | est | unblocks N | prio
2  pg show ID                          # done criteria, deps, evidence kinds (~300 tokens)
3  pg claim ID --as L1                 # atomic; 409 if taken -> next line of step 1
4  pg token issue --item ID --role writer   -> spawn L2/L3 via autoos-agent with card=ID
5  pg heartbeat ID                     # while the worker runs
6  worker: pg evidence ID --commit SHA --ci URL --run RUN ; pg submit ID
7  reviewer (other family): pg verdict ID --pass | --changes "reason"   # keeper verifies, sets done
8  goto 1.  pg alerts --project P  every loop
```
**Escalate to routing-00 only when:** lease expired twice on one item (cut-run rule), evidence refused twice, review
"changes" twice, item blocked > 4 h by *another project's* item, ready set empty while goal unreached and nothing in
flight (stall/deadlock), a cycle/validator error, a definition change touching another project, keeper unreachable
> 10 min. **routing-00 via the graph:** `pg ready --all --brief`, `pg assign ID --to L1`, `pg priority ID +n`, `pg digest`
(counts per project, stalled items) — assignment by id, not by long messages.

## 5. Human page (req 3)
`pg render` → one static HTML from the snapshot: Cytoscape.js + dagre/ELK **vendored in the repo** (no CDN at runtime),
left-to-right start → parallel branches → goal; colour = state (open/ready/claimed/in review/done/blocked); click =
detail panel (owner, model, evidence links, corrected-by); project filter chips; critical path highlighted; DONE
collapsed; on phone widths the first tab is the ready / blocked-by / in-progress list. Read-only: no write path from the page.

## 6. Pilot (after the choice) · tests · cost
**Pilot:** import plan v3 (84 lane ids found in T0–T7 vs "~76" in STATUS; owners from file headers; dependencies from
the Order sections + phase gates — extracted by an L2 writer into a reviewed `plan-v3.import.yaml`, never auto-trusted);
legacy "done ✓" rows without a sha import as `in_review` (flagged), not DONE. Computed views: ready, blocked-by,
waves (graphlib layers), critical path by estimate, % done. CLI + MCP + routing hook. ~900–1,200 lines in ≤ 400-line files.

**Tests (TESTING.md style):** (1) attack tests — as an agent try edit/delete/reopen/rewrite-`corrects` of a DONE item
through CLI, MCP, raw SQL on the file, forged actor, replayed outbox → all refused and logged; hash-chain tamper
detected; (2) 3 agents claim one item concurrently → exactly one wins, lease expiry reaps; (3) evidence: bogus sha, sha
not on main, red CI → refused; (4) cycle / unreachable goal refused at write; (5) import → export → diff identical;
(6) token cost: `pg ready --brief` vs reading the plan files (today ≈ 54.7 kB ≈ 14 k tokens by bytes/4 — an estimate;
both sides measured with one tokenizer in M3); (7) page screenshots desktop 1280 px + phone 390 px (browser run →
workstation); (8) failure drill: kill the keeper → read-only fallback, outbox replay; (9) one live L1 (autoos) works a
real lane from the graph with zero prose orders.

**Cost/time:** M2 (pilot running) ≈ 1.5 days, M3 (scorecard) ≈ 2.5–3 days; ≈ 0.6–1.0 M Claude tokens (L1 Sonnet +
finals), writers/reviewers on trial/free legs; no new server (SQLite on coding.vm; browser tests on the workstation —
placement request at test 7). Ongoing: ours to maintain; dependencies = Python stdlib (sqlite3, graphlib), Cytoscape.js
(MIT) + layout plugin (licence/maintenance to verify day 1), MCP Python SDK.

## 7. Decision requested
1. **C (default)** vs B (keep Beads engine, add keeper) vs another row after the sweep.
2. SQLite for the pilot, Postgres decided at M4.
3. Private remote for this repo (backup)? Default: no until M2.
