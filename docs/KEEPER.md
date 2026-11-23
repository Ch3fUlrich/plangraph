# Keeper contract (M2a) — store, state machine, evidence, API, CLI, MCP

Store snapshot filters (S1): `open=1` / `closed=1`, UTC `since`, item `fields` (always `key`), and `blocks`; nonblank filters add selection/returned counts in `filter`. Activity uses only `since`; aliases and progress stay full; decision text follows selected evidence before field projection. No filters preserves legacy bytes; HTTP wiring is S2. Signed snapshots, serve and pull stay full.
Builds on docs/SPEC.md (item shape, views) and docs/PROPOSAL.md §3. Python 3.12 **stdlib only** (sqlite3, http.server, hashlib, secrets, json, graphlib, subprocess, urllib); PyYAML only in the importer. Package `src/plangraph/keeper/`. Every file ≤ 400 lines, tests first (pytest), no network in tests (fakes), no secrets in code/logs/tests. M2a = per-principal tokens; M2b (later) adds run-bound tokens, signed snapshot, outbox, anchor — leave clean seams, do not build them. System-only `project_state` atomically projects execution ownership and lifecycle state; see [exec ownership](adr-exec-ownership.md) for supersession and stale-run retries.

## Modules
| file | content |
|---|---|
| `schema.py` | DDL + triggers as SQL strings; SQLite `SCHEMA_VERSION` = 25 and PostgreSQL migration 25 ([landing storage and settlement](keeper-postgres.md#schema-and-migrations)), `OVERSIGHT_SCHEMA_VERSION` = 14 (v14 = the oversight `OVERSIGHT_DDL` appended after main's `JEV_DDL`, installed by `db._upgrade`: tables/indexes presence-based, the four oversight **guard triggers dropped and re-created on every open** so a weakened body cannot survive — SQLite's open path; Postgres installs the same guards once in Migration 8, GAP: no per-open repair there, D-2458); main's #140 exec ownership columns (`items.exec_workflow_id/exec_run_id/exec_attempt/exec_principal/exec_retired_runs`, also `db._upgrade`) stay in the `items` DDL at main's v13 shape, PG Migration 7 |
| `db.py` | `open_db(path, *, create=False) -> sqlite3.Connection`: the ONLY way the keeper opens the DB. Sets `foreign_keys=ON`, `recursive_triggers=ON`, WAL, `busy_timeout`, `trusted_schema=OFF`, and `set_authorizer` denying DDL (`CREATE/DROP/ALTER`, `DROP TRIGGER`), `PRAGMA writable_schema`, `ATTACH`, `load_extension` after the schema is installed. Schema install uses a separate `create=True` path. Singleton `flock` helper `lock_keeper(path)`.  Optional Postgres backend (`backend: postgres`, `dsn`/`PG_DSN`): see `docs/keeper-postgres.md`. |
| `events.py` | event append with hash chain (`hash = sha256(prev_hash + canonical_json(event))`), `fold(events, unknown=None) -> state`, `verify_chain(conn) -> list[problem]`, `verify_integrity(conn)` (cache `items.state/epoch/claimed_by` equals fold + one `unknown_event_type` problem per event `type` outside `EVENT_TYPES`) |
| `auth.py` | principals + tokens: `add_principal(conn, name, role, projects, family=None) -> token` (shown once; only sha256 stored), `authenticate(conn, token, *, now=None) -> Principal`, `revoke`. Roles: `router`, `l1`, `worker`, `reviewer`, `system`, `reader`, `spawner`, `oversight`, `phase-worker`, `sweep`. `add_oversight_principal(conn, name, projects)` provisions the revocable row (no token); a `pgo1_` token authenticates as `OversightPrincipal` **and is refused there once `expires_at` has passed** (the TTL is checked at authentication, judged by `now` or by wall time (`time.time()`); dispatch resolves the principals table first, so a prefixless token whose bytes start `pgo1_` or `pgh1_` (the host-sample namespace) still resolves as itself, and `add_principal` re-rolls any draw that would). `add_principal` refuses `role="oversight"`. `sweep` is an unscoped read-only role (`whoami`, `snapshot`, `show` only; all writes and unrelated reads return `403 forbidden`). |
| `store_oversight.py` | `OversightMixin`: local-only `mint_oversight_token(name, go_decision=…, reason=…)` (TTL 3600 s, generation rotation, sha256 only; `prepare_oversight_token`/`commit_oversight_token` re-validate the live env, the GO decision row, the TTL bound and the prepare's own digest at commit), the per-use token re-check, the derived hourly budgets (**consulted by every writer before it inserts**), and the `oversight_ops` audit rows + one open `oversight-guard` alarm per principal. |
| `oversight_policy.py` | pure policy: `check_action(verb)` and `check_batch(items_by_key, ops, projects) -> [Verdict]` (the single grant, project-scoped, used by both fail-closed seams; oversight may create none-tier questions with mixed decision/commit evidence, while decision-only declarations require the router; malformed op data answers with a fail-closed `malformed` verdict, never an exception). |
| `store.py` | the verbs (below) as methods of `Store(conn, clock=time.time, verifiers=…, ctx=…, batch_roots=…)`, mixed in from `store_create`/`store_verbs`/`store_review`/`store_waiver`/`store_close`/`store_read`/`store_reap`/`store_support`; every verb takes `(principal, …)`; Postgres-only host samples (`store_host_samples`, `host_samples_rollup`): [KEEPER-HOST-SAMPLES.md](KEEPER-HOST-SAMPLES.md) |
| `store_close.py` | `CloseMixin`: the router-only `close_on_decision` verb for tier-`none` items (X7b, "Legacy and decision closes") |
| `evidence.py` | pluggable verifiers (below) |
| `importer.py` | YAML dir → router-attributed batch; `export_yaml(conn, dir)` for round trip |
| `api.py` | HTTP JSON server around `Store` |
| `cli.py` (`python -m plangraph.keeper.cli` / entry `pg`) | thin client of the API; `cli_admin` and oversight's `db` import are lazy, so remote `whoami` works without POSIX `fcntl` on Windows; local DB commands still require the keeper host; `oversight-check` and `oversight-audit --out-dir DIR --cursor FILE --router-log FILE` live in `cli_oversight.py` |
| `mcp.py` | stdio MCP server (hand-written JSON-RPC 2.0: `initialize`, `tools/list`, `tools/call`; no SDK), same verbs, calls the API; unchanged by the [Rust schema-only slice](keeper/mcp-rust.md) |

Keeper startup warns once when `repos.decisions` is missing; new devstack configs require `PLANGRAPH_DECISIONS_REPO` and emit its checkout path with ref `origin/main`, while existing valid configs are reused if it is unset and explicit updates preserve unrelated settings (see [install config](RUNBOOK.md#1-init-and-config)). The daemon publishes [loaded-code identity for guarded main fast-forwards](keeper/main-fast-forward.md) beside its DB anchor, and removes it on clean shutdown. Critical-path stall alerts notify owners and routing without mutating keeper state; see [configuration and delivery semantics](keeper/crit-path-stall.md). [Repository mirrors](repo-mirror.md) are a standalone `pg-ctx` Rust slice; keeper mirror wiring and scheduling/timers are later slices, not daemon startup.

Backlog consumers can run `merge_batch.py consumed --repo OWNER/NAME --pr N --head SHA`.
The keeper matches the exact folded head in a verified landed train.

A lane can appear in `pg ready` and be claimed when one open train and its open ancestors contain accepted parts for every unfinished dependency. Each acceptance binds a review run to the item and exact part head. The ready response and claim reply expose the selected train's `base_sha`.
SQLite v25 and PostgreSQL migration 25 index item definition events for claim preflight.

SQLite schema v22 and PostgreSQL migration 21 add `landing_claim_bases` to pin the train and supporting parts to the claim epoch. If the train head changes, a supporting part is dropped, or a supporting train closes unmerged, the keeper records a `released` event with the reason, clears lane ownership, and returns the item to `open`. The item appears in `pg ready` again when its dependencies qualify.

The lander checks dependencies before merging. When settlement verifies a merge on main, the keeper records the actual included parts and keeps the first unmet dependency as `dependency_warning` in the landing journal. Settlement can then finish and the warning remains visible in the journal.

## Tables (all in one SQLite file)
`principals(name PK, role, projects json, family, token_sha256, active)` ·
`items(key PK, project, id, title, kind, track, owner, estimate_h, estimate_src, done_criteria, evidence_kinds json, operator,
priority, source, note, description, host, corrects, state, epoch, claimed_by, delegate, lease_until, reviewer, model, created_at, updated_at)` ·
`edges(src, dst, rel CHECK rel IN ('depends_on'))` (`corrects` lives on the item; `src` depends on `dst`) ·
`events(seq INTEGER PK AUTOINCREMENT, ts, actor, role, model, key, type, payload json, prev_hash, hash)` append-only ·
`evidence(id PK, key, epoch, kind, ref json, verified, detail, at)` ·
`audit_log(id PK, ts, actor, role, action, key, ok, code, msg)` — **every** attempt incl. refused ones; never locked ·
`audit_findings(id PK, ts, key, code, msg)`.
Event types: `item_created, item_defined, claimed, delegated, released, lease_expired, evidence_added, submitted,
changes_requested, review_passed, cancelled, priority_set, kind_set, incident_appended`. `items.state`, `epoch`, `claimed_by`, … are a CACHE written in the same
transaction as the event; `verify_integrity` proves cache == fold. Retired exec run fences are uncapped per card and survive close; delayed parks without an observed state cannot overwrite newer attempt state writes, and park state moves follow the forward table while legal parks release claims. See `adr-exec-ownership.md`.

**Unknown event types (PG-FOLD-UNKNOWN-TYPE).** A `type` outside `schema.EVENT_TYPES` was written by a newer keeper: this build cannot
replay it, so `fold(events, unknown=…)` records `{"seq","type","key"}` in an optional side channel and never touches item state (the
event is otherwise skipped, so the cache cannot drift and an old rebuild cannot undo it). `verify_integrity` turns each entry into one
problem `{"key": key-or-None, "code": "unknown_event_type", "field": "type", "expected": None, "got": kind, "seq": seq}`; `pg admin
verify` prints it as `KEY unknown_event_type type=… seq=…` (`-` for a keeper-level, keyless event). Types in `EVENT_TYPES` with no fold
branch — `review_verdict`, `token_minted`, `outage_recorded` — are known no-ops (replayed by `token_fold`/the store) and are **never**
reported as unknown. `oversight_token_minted` is **keyless**, so `fold` returns before its
branch: replay does **not** rebuild `oversight_tokens` (GAP: that ledger is revoke-only
table state, never an event-sourced cache — read it from the table, not from the chain).

## Oversight principal (RT-OVERSIGHT-KEEPER-SCOPE)

An `oversight` principal is a short-lived, scoped identity for a human/agent that
may only *refine* the plan, never restructure it. It is provisioned with the local
`auth.add_oversight_principal(conn, name, projects)` (non-empty `projects`; empty
means nothing, R5) and its token is minted only by the local admin CLI:

    pg admin mint-oversight-token NAME --go D-NNN [--reason TEXT] --token-out FILE

Minting requires `PG_OVERSIGHT_LIVE=1` on the host **and** a `decisions` row
`D-NNN` with `kind='operator'` carrying the whole `oversight-live` scope in
`waives` (R2: no HTTP/MCP verb; default deployment has no live token). There is
no extension: the token expires after 3600 s; minting again revokes every live
token of the principal and starts generation N+1. The plaintext is returned once,
written 0600 to `--token-out`, and never stored, logged or printed.

The verb seam (`Store._run`) lets an oversight principal call reads and exactly
one write verb, `apply_ops`; every other write verb is `403
oversight:verb_denied`. Inside `apply_ops` (batch_ref is required) the pure
policy allows only:

* `create` a `lane`/`question` with explicit `review_tier` (`standard`, or
  `none` only for `question`), non-empty `done_criteria`, and the closed
  allowlist of item fields (no `under`, no lifecycle fields). The tier is
  **raise-only**: an explicit tier *below* the project's `default_review_tier`
  is refused `403 oversight:tier_below_default` — it is never silently stored
  as the default — while a tier at or above the default is honoured exactly as
  asked;
* `edit` `note`/`description` and **add** `depends_on` edges on an OPEN,
  non-high, non-gate/goal/decision/operator_task item;
* `priority` on such an item;
* `add_dep` on such an item to an existing non-cancelled dep (a DONE target is
  legal; a cancelled one is `oversight:dep_cancelled`).

R5 bounds **every** one of those ops to the principal's `projects`: the item a
create/edit/priority/add_dep names, the item a create would insert, and every
dependency target (create `depends_on`, an edit's added deps, `add_dep`'s dep)
must be inside the grant; anything else is `403 oversight:project_denied` and
nothing is written. The same `projects` value is passed to
`oversight_policy.check_batch` at both fail-closed seams (pre-lock and in-lock).

Dependency removal (including add+drop in one list), cross-project items or
dependencies, high/decision/operator_task targets, tier edits, `done_criteria`
edits, `under`, unknown or malformed ops/fields and cycles fail closed —
malformed op data (a list where a name belongs, an unhashable `kind`) answers
with a fail-closed policy verdict and a refusal, never an exception. Each
attempt writes one `oversight_ops` row (applied inside the batch transaction,
denied after its rollback — a **denied `dry_run`** writes no *applied* row but
does record its denial) and a denial also raises one open `projector_alarms`
row (`kind='oversight_denied'`, `workflow_id='oversight-guard'`, keyed by the
**principal**: one open row per principal however the op's `finding` ids
rotate — the initial finding is preserved in detail on conflict). A refusal at the
verb seam **after** the token row was resolved — a denied verb/unknown verb, or
a revoked/expired/rotated/scope-drifted token — is audited the same way, naming
the attempted verb and key with `finding_id=''` (no op carried a finding), and
counts toward the denied budget. When an active oversight token has expired at
`authenticate`, its denial is audited and counted toward the hourly budget before
refusing; only requests where no principal is resolved at all (unknown token hash,
revoked-at-auth hash, inactive principal row, role drift) remain unaudited in
`oversight_ops`; they still write the `audit_log` authentication row. Applied/denied per-hour caps are derived
from `oversight_ops` and **every writer consults them before it inserts**, so a
breach is `403 oversight:rate_limited` — its denied row (when the cap still
allows one) carries a human-readable reason — and alarm-only: only an explicit
`pg admin revoke NAME` kills the identity (R4).

## Locking (prevention)
Triggers key off `EXISTS (SELECT 1 FROM events WHERE key=… AND type IN ('review_passed','closed_on_decision'))` (never the cache) — the
two events that finish an item (X7b adds the router's decision close beside the passed review), and also treat
`cancelled` as locked: `BEFORE UPDATE/DELETE` on `items` (any column except none — DONE rows are fully frozen), on `edges` where
`src` is locked, on `evidence`/`events` of a locked key; `BEFORE INSERT` on `events`/`evidence`/`edges` for a locked key, and on
`items` when the key already exists in a locked state (fires for `INSERT OR REPLACE`). New items MAY depend on locked/DONE items
and may set `corrects` to a DONE key (validated by the store: target must be `done`). Events table: `BEFORE UPDATE/DELETE` always abort.
`principals`/`audit_log` are not locked.

## Verbs (Store) — role rules, state machine, errors
Every verb: authenticate → authorise → validate → one transaction (event + cache + edges) → audit_log row (ok or refused).
Refusals raise `Refused(code, msg)`; codes: `forbidden, not_found, bad_state, bad_transition, locked, epoch_stale, lease_held, cycle, validation,
evidence_missing, evidence_unverified, same_principal, same_family, family_unknown, writer_unknown, coverage, conflict, exec_stale, exec_stale_state`. A refused attempt writes only `audit_log`.
`exec_stale`/`exec_stale_state` and `bad_transition` are the exec-ownership fence (see `docs/adr-exec-ownership.md`): a stale or retired run, a
worker that is not the recorded owner, a card whose observed state fails the live-set CAS, and a back move on an OWNED card without
`run`/`hand_back`. Recording an owner is `forbidden` for every principal but a worker (the recording principal is stored in `items.exec_principal`).
| verb | who | effect |
|---|---|---|
| `create_items(p, items)` | router (any project); l1 (own projects) | batch of definitions as `item_created` events (state `open`); whole batch validated with `views.validate` over existing+new (cycle, unknown dep, unreachable…) or refused atomically; optional per-item `under: <key>` attaches the gate → new edge in the same transaction (paragraph below) |
| `import_items(p, items)` | router only | like `create_items`, but per-item `state` ∈ {open, claimed, in_review} is honoured (legacy rows, e.g. work already in flight): the item gets `item_created` plus a `claimed`/`submitted` event flagged `legacy: true` with `claimed_by` = the item's `owner` string (need not be a principal) and NO verified-evidence requirement; `done` is never importable; the router later attaches evidence (e.g. `decision`) via `add_evidence_router(p, key, kind, ref)` (router-only, state `in_review`/`claimed`, still runs the verifier) |
| `edit_definition(p, key, fields)` | router; l1 own project | only while `open`; fields: title, description, host, estimate_h, estimate_src, done_criteria, evidence_kinds, depends_on, track, owner, note; `estimate_src` is a non-empty string (≤64 chars, credential-guarded) and needs a non-null `estimate_h`; clearing `estimate_h` (null) also clears `estimate_src`, and a non-null `estimate_src` together with a null `estimate_h` is refused; an `estimate_h` edit that omits `estimate_src` auto-fills it with the editing principal's name (one `estimate_src_auto` marker in the `item_defined` payload), so the item stops reading `default`; cross-project `depends_on` changes: router only; a `depends_on` entry naming a `cancelled` item is dropped from the list (result carries `warnings`, not a refusal); whole-graph validate; `item_defined` event |
| `set_priority(p, key, n)` | router | `priority_set` |
| `set_kind(p, key, kind)` | router | PG-SET-KIND: an `open` `lane` becomes a `question` or `operator_task` (`pg set-kind KEY KIND`, MCP `set_kind`, `apply_ops`/batch op `set_kind`); any other target kind, source kind or state is refused (`validation` / `bad_state` / `locked`). One `kind_set` event `{kind, was, operator, wait}`; the row gets `operator=1` and the kind's default `wait` (`answer` / `task`; an explicit `wait` is kept), as `create_items` would store. `fold` replays `kind`, `wait` and `operator`; `verify_integrity` compares `kind` and `operator` for every item that has a `kind_set` event; the HTTP/MCP door refuses fields other than `key`/`kind` |
| `migrate_descriptions(p)` | router | moves a leading folded `description: ` note line into the `description` column of every active item (never DONE/cancelled, never overwrites); one `item_defined` event per item; idempotent. `alerts` also lists advisory `warnings` (`title_is_id`, `no_description`) |
| `rehome(p, key, new_project, host=None)` | router | moves an `open` item to another project in one transaction: creates `new_project:<id>` (`item_created` with `rehomed_from`, copies the definition and deps, `host` kept when omitted), re-points every unlocked dependent edge (one `item_defined` each), cancels the old item with reason `rehomed to <new key>` and records an append-only ALIAS `old -> new` (`pg aliases`, `GET /v1/aliases`). DONE/cancelled items are `locked`, claimed/in-review items `bad_state`; locked dependents keep their edge to the old key (the alias resolves it). Aliases resolve in the API (`key` fields), `apply_ops` (op `rehome` too), `depends_on`/`corrects`, the offline CLI snapshot and `Closes:`/`Waives:` tokens. `verify_integrity` checks the alias table against the `rehomed_from` events (`alias_mismatch`) |
| `claim(p, key, lease_s=7200)` | l1 (own project), worker | `open` with satisfied dependencies → `claimed`, epoch+1, `lease_until`; atomic (BEGIN IMMEDIATE); second claimant gets `conflict` |
| `delegate(p, key, worker_principal)` | claimant (l1) | sets `delegate` (the worker principal; it must have role `worker`) |
| `heartbeat(p, key, epoch)` | claimant or delegate | extends lease; stale epoch → `epoch_stale` |
| `release(p, key, epoch)` | claimant, router | `claimed`→`open` |
| `add_evidence(p, key, epoch, kind, ref)` | claimant or delegate, state `claimed` | runs the verifier for `kind`; stores `verified` + detail; unverified evidence is stored but does not count |
| `incident(p, key, text, evidence=None)` | router or L1 in its project | `pg incident KEY --text TEXT [--evidence URL]` appends an attributed, timestamped report to a live item. Read the log in show, snapshot or the item card. PostgreSQL migration 22 and the SQLite v23 upgrade add the event index. |
| `submit(p, key, epoch)` | claimant or delegate | `claimed`→`in_review` iff every kind in `evidence_kinds` has verified evidence; lease frozen (cleared) |
| `request_changes` / `pass_review` as `verdict(p, key, ok, reason)` | reviewer | `in_review`: ok → records `review_verdict` and re-verifies ALL evidence; on a `standard` item this one cross-family verdict → `done` (event `review_passed`, locks), on a `high` item a second distinct reviewer (two distinct resolved families unless waived) is required; not ok → `claimed` (same epoch; 2nd time in a row → `open` + audit_finding). Refused: reviewer == claimant/delegate (`same_principal`); reviewer.family == delegate-or-claimant family (`same_family`); either family NULL (`family_unknown`, fail closed) |
| `waive_second_family(p, key, D-NNN)` | router | **`high` items only (D-228)** — `claimed`/`in_review`: runs the `decision` verifier on `{id: D-NNN}` **and** requires that D-row to contain the whole token `Waives: <key>` (X6c3); on success emits `waiver_set` and sets `items.review_waiver`. An active D-id waiver permits one passing verdict to close, with writer independence and single-seat sensitive coverage intact (D-5658), **except cards tagged `auth` or `secrets` still require two distinct non-Anthropic families (`need=2`)**. Refused: non-router (`forbidden`), unverified decision or a D-row that does not name the key (`evidence_unverified`), `standard` or `none` tiers (`bad_state`), DONE (`locked`), other states (`bad_state`) |
| `apply_ops(p, ops, batch_ref=None)` | router; a granted l1 (ops `create`/`edit`/`cancel`/`priority`/`add_evidence`/`close_on_decision`/`legacy_state`); oversight narrow grant | B1: BATCH.md ops in one transaction, all or nothing (`store_ops.py`; paragraph below) |
| `grant_apply(p, name, project)` / `revoke_apply` / `apply_grants(p)` | router (`apply_grants`: router, or an l1 for its own) | KEEPER-PROJECT-SCOPED-L1-APPLY: the `apply_grants` table (principal, project) says which projects a role-l1 principal may batch-apply in; `pg grant-apply NAME PROJECT [--revoke\|--list]`. One key outside the granted projects (item, `depends_on`, `under`, `corrects`, `fixes`, target) refuses the whole batch naming it with `forbidden`; `rehome`/`set_kind`/dep ops stay router-only; every applied batch posts one `granted_apply` line (batch, source, sha256, ops_sha256, op count) to `routing` |
| `close_on_decision(p, key, batch_ref)` | router only | **`review_tier='none'` items only (X7b)** — see "Legacy and decision closes" |
| `cancel(p, key, reason)` | router; l1 own project | `open|claimed|in_review`→`cancelled`; refused from `done` (`locked`) |
| `reap(now)` | system | `claimed` with `lease_until < now` → `open` (`lease_expired`); `in_review` stuck > 4 h → audit_finding `review_overdue` (no state change) |
| `audit_meta(p, project=None, stale_days=3)` | router (any project); l1 (own projects) | read-only plan-metadata audit: missing `description`/`estimate_h`/`done_criteria`/`owner` + stale claims. See "Plan metadata audit" |

`python3 tools/names_backfill_dryrun.py SNAPSHOT.json [--json]` is the MODEL-NAMES dry run: it reads a plan snapshot
JSON and only lists the active items whose `alerts` warnings are `title_is_id` / `no_description` (sorted line per
finding: current title, a proposed title derived from the key, the head of the note's folded `description: ` line or
`MISSING`, then `count:`) — the LIVE backfill remains the separate router step `pg migrate-descriptions`.
**`under` (PG-L1-CREATE-WITH-EDGE, `create_items` only).** A definition may carry an optional
`under: <key>` naming an *existing* gate/goal item G: the new item N is created and, in the SAME
transaction, `edges(src=G, dst=N, rel='depends_on')` is inserted — G `depends_on` N — so N reaches the goal
without a router edit afterwards. The result gains `attached: [{key, under}]`; besides N's `item_created`,
G gets one `item_defined {fields: [depends_on]}` event and an `updated_at` touch (the fold ignores that
payload and edges are not folded, so `verify_integrity` stays green). A bare id resolves like any key
reference (`_qualify`: the NEW item's project, then the alias table). Rules, all before any write: G must
exist (`not_found` — a key created earlier in the same batch does not exist yet and is refused), the caller
must manage G's project (`_may_manage`: router any project, l1 only its own → `forbidden`), a non-router may
only attach inside the new item's own project (a forged cross-project `under` is `forbidden` even when the
l1 manages both), and G must be neither DONE nor cancelled (`bad_state`; any other lock `locked`). The
candidate graph — every gate with G modified to `depends_on + [N]` — goes through `views.validate` first, so
a cycle or a stranded new item is refused atomically and nothing is created. `under` is create-only:
`import_items` refuses it (`validation`), `edit_definition` does not accept it (`validation`), and because it
is a `KNOWN_ITEM_FIELDS` name it never folds into `note`.

**D-228 review tiers (operator; supersedes the single-verdict `done` above, and the flat "exactly TWO reviewers" reading of D-219):** how many passing verdicts `done` needs is a per-item `review_tier` (`'standard'`|`'high'`|`'none'` — `none` never gets a verdict at all, it is closed on a decision; see "Legacy and decision closes"), named at definition time by the **L0 router** or the **L1 project orchestrator** (`create_items`/`import_items`, `edit_definition`) — **only the L0 router may lower it** (a non-router `high`→`standard` edit is `forbidden`; raising is open to L1) **and only the L0 router may name `none`** (a non-router `none` is `forbidden`). The **L2 phase orchestrator** never sets or changes a tier; reviews belong to the **L3 task group** (writer + reviewers). With no explicit tier, `default_review_tier` picks `high` for project `plangraph` and for any item whose id/title/track matches a risk keyword (`secret(s)`, `key(s)`, `security`, `gateway`, `omniroute`, `deploy`, `keeper`, `fence`), else `standard`.

Both tiers record one `review_verdict` event per passing verdict (current epoch/submission round) and re-verify ALL evidence with the same logic inside the same write lock, so a stale-evidence refusal never depends on which seat spoke first. `standard`: **one** passing verdict is enough — the reviewer must be a distinct principal (not the claimant, not the delegate) whose normalised non-blank family differs from the writer's (`_reviewer_family_ok`, fail closed on a blank family on either side) — and the item goes `done` + `review_passed` immediately. Unwaived `high`: D-219 — one implementer plus exactly TWO reviewers, no extra Sonnet final; `done` only when two distinct reviewers from two distinct RESOLVED families have both passed, a third verdict is refused (`conflict`). Waiver path (D-217, D-5658): router-only `waive_second_family(key, decision_id)` — requires a verified `decision` record D-id **whose DECISIONS.md row contains `Waives: <project:ID>` for exactly that key** (X6c3: whole-token match, so `Waives: demo:A` does not waive `demo:AB`; one D-row may list several `Waives:` tokens; tokens inside fenced/indented code blocks do not count). With an active D-id waiver, one passing verdict satisfying `_reviewer_family_ok` and `_check_coverage(need=1, sensitive=True)` closes `high` via `review_passed`, reporting only actual passing verdicts in `reviews_passed`; the waiver is not a verdict. **Auth/secrets exception:** a high item's card `risk_tags` containing `auth` or `secrets` keeps `need=2` even under a waiver: two distinct resolved non-Anthropic seat families and two-seat writer coverage are required; aliases do not create independent seats. Risk tags are validated inside the verdict write lock using the same rule as `close_merged`; malformed stored tags refuse `validation: card_risk_tags_invalid`, and insufficient non-Anthropic families refuse `coverage` without appending a closing verdict. It is recorded in the event and exposed by `show`, `digest` and the snapshot (`review_waiver: D-217`); `standard` and `none` waivers remain refused (`bad_state`). A `verdict(ok=False)` from either seat of either tier returns the item to `claimed` and voids the round's verdicts. Per-item `reviews_required` remains fixed at 2 for `high` and 1 for `standard` (not editable); the waiver changes only the closing threshold outside the auth/secrets exception.
Allowed edges only: open→claimed, claimed→open, claimed→in_review, in_review→claimed, in_review→done, open|claimed|in_review→cancelled. Invalidating an open-train base also allows in_review→open.
Reads (any authenticated principal): `ready(project=None, n=10)` (text `views.brief`), `show(key)` (compact: fields, deps with states, unblocks,
evidence, lease; ≤ ~300 tokens), `blocked(project)`, `waves()`, `critical_path(project)`, `percent(project)`, `alerts(project)` (lease expiring < 15 min,
overdue reviews, audit_findings, stall = no ready + nothing in flight + goal unreached, refusals ≥2 on a key), `digest()` (router: per-project counts, stalled, last
10 DONE with evidence kinds), `snapshot()` (SPEC snapshot JSON; `head` = last event hash; persisted fleet: see docs/fleet-snapshot.md), `whoami()`.

## Evidence verifiers (`evidence.py`)
`Result(ok: bool, detail: str, ref: dict)`; registry `VERIFIERS: dict[str, Verifier]`; `Verifier.verify(item, ref, ctx) -> Result`; ctx holds
injected adapters so tests use fakes: `ctx.git`, `ctx.ci`, `ctx.runs`, `ctx.semaphore`, `ctx.decisions`, `ctx.now`.
| kind | ref | passes when |
|---|---|---|
| `commit+ci` | `{commit, repo?, ci_url?}` | repo = registered path of the project (`repos` config, router-owned); commit exists and is an ancestor of `main`; commit message has trailer `Plan-Item: <key or id>`; commit time ≥ the claim time; CI check-runs for exactly that sha have conclusion `success` (adapter wraps `gh api repos/{o}/{r}/commits/{sha}/check-runs`; the merged-PR proof reads `gh api --paginate .../commits/{sha}/pulls` page arrays, no `--slurp` (gh 2.45); a `forgejo:<owner>/<repo>` value reads Forgejo commit statuses instead — see "Forgejo CI statuses" below; `ci: none` allowed only if the project config says it has no CI) |
| `run-record` | `{run_id}` | file `<runs_dir>/<run_id>/job.json` exists, its `request.title` or `task` text contains the item id or key, and `<run_id>/exit.json` has `rc == 0` (format: `job.json` = `{id, run_id, request{card,title,model,…}, task}`, `exit.json` = `{rc, ended}`); runs_dir is config |
| `run-record` | `{run_id, host, writer}` | a completed spawner `record.json` has exit code zero and matching run, host, item and writer leg. Keeper reads the local host's record from `runs_dir` or the spawner state directory and reads remote records from `spawner_records_dir`. Local review-seat discovery uses the same record reader. Set `PLANGRAPH_HOST` in Keeper's environment when the spawner uses an explicit host ID. |
| `semaphore-task` | `{task_id, project_id, template}` (all positive integers; `template` = template id, REQUIRED, a name is refused) | adapter `GET {base}/api/project/{project_id}/tasks/{task_id}` returns status `success`, `id`/`project_id` matching the ref, and `template_id` equal to `template` (a response without `template_id` fails); the project must be listed in env `PG_SEMAPHORE_PROJECTS` (comma-separated positive ints; unset/empty/malformed fails closed with "semaphore project allowlist not configured", otherwise "project not allowed", before any request); HTTP 401/403 report "semaphore auth refused (code)", other HTTP errors "semaphore request failed (HTTP code)"; real fetcher reads base URL from env `PG_SEMAPHORE_URL` and the token from the file named by env `PG_SEMAPHORE_TOKEN_FILE`; the token value is never logged or returned; endpoint `/api/project/{project_id}/tasks/{id}` (Semaphore UI API) |
| `decision` | `{id: "D-209"}` | line `| D-209 |` exists in the freshly fetched `origin/main` log; `decisions_file` locates its repo and relative path (e.g. `router/docs/decisions-log.md`). Each verification fetches origin main with `+refs/heads/main:refs/remotes/origin/main` (including rewritten history), then reads `git cat-file blob origin/main:<path>`; directories and other non-blobs refuse. The checkout need not advance or contain the file. `DecisionsFetchFailed` / `DecisionsOriginUnreadable` name fetch / repo-ref-blob failures; no stale-file fallback or checkout-metadata cache. |
| `commit` (X7b, legacy) | `{commit}` | the sha exists **and** is an ancestor of `main` in the project's registered `repos` path — and *nothing* else: no `Plan-Item` trailer, no claim-time window, no CI. Router-only attachment (see below) |
`evidence_kinds: [any]` = at least one verified evidence of any kind. Unknown kind → `validation`.

### Forgejo CI statuses (PG-CI-FORGEJO)

`commit+ci` binds the item by the `Plan-Item` trailer of the commit; the trailers of a message are every trailing paragraph made only of `Key: value` lines (git reads only the last, so a `Co-Authored-By` paragraph of its own would hide the `Plan-Item` and `Writer` paragraph before it). A merge commit that carries none (a merge made in the hosting UI) stands for the commits it introduced (everything outside its first parent, nested merges included, never a later commit): a trailer of any of them naming the item binds it, and CI is read on the merge sha itself, and a commit with neither is refused `does not carry trailer 'Plan-Item: <key>'`. `tools/merge_batch.py land` merges the batch PR with one `Plan-Item` trailer per item of every included part in the merge commit body.

A `ci` value of `forgejo:<owner>/<repo>` routes `commit+ci` away from `gh` and into
`src/plangraph/keeper/evidence_forgejo.py`, which reads Forgejo commit statuses: `GET
{base}/api/v1/repos/{owner}/{repo}/commits/{sha}/statuses?page=N&limit=M` (operationId
`repoListStatusesByRef`; `sort` is left default so newest-per-context is decided locally).
`base` comes from the `forgejo_url` config key (`https` only, `http` accepted solely for
`127.0.0.1` in tests, no embedded credentials); the token comes from `forgejo_token_file`,
re-read on every call and refused when missing, empty, a symlink or not a single token. It
is sent as `Authorization: token <T>` and never appears in argv, the URL, logs, a raised
message or `repr`. Both keys are needed to build the adapter — with either missing the
`forgejo:` value fails closed in `CIAdapter.check_runs`. stdlib `urllib.request` only (no
subprocess, no redirect replay), hard timeout, body read in chunks under `max_bytes`, at
most `max_pages` pages of `page_size`: a full page at the cap raises instead of returning
a truncated list, and any HTTP error / non-JSON / non-list payload raises with the status
code and `owner/repo` only. Each `CommitStatus` (`id`, `status`, `context`, `created_at`,
`updated_at`) is normalised into the check-run shape the verifier already understands
(`context` → `name`, `app.slug = forgejo-actions`, `head_sha` = the requested sha, `pending`
→ `in_progress`/no conclusion, `success`→`success`, `failure`/`error`→`failure`,
`warning`→`neutral`, `skipped`→`skipped`, an unknown status → `failure`), keeping only the
newest status per `context` (`updated_at`, ties by higher `id`) so the stale failure of a
rerun cannot block a green sha; no statuses is the verifier's existing "no CI check-runs"
failure. Proposed, not applied: `docs/proposals/keeper-ci-config.md`. The decisions-origin fetch in `evidence_decisions.py` runs in an isolated git env (no system or global config). It re-injects only the caller's global `credential.helper` and `credential.<url>.helper` entries through `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n`, so a private https origin authenticates; values are never logged and a failed read injects nothing. Includes are explicitly disabled while reading helpers; helper order and empty reset entries are preserved. Other credential settings, core, URL, HTTP and include entries stay isolated. A timed-out reader is killed and reaped. The door is `src/plangraph/keeper/evidence_decisions.py` function `_origin_git_env`; regression coverage uses an authenticated loopback smart-HTTP origin and a fixture-only helper.

## Legacy and decision closes

X7b covers the two ways work leaves the system without ever going through a review: a
historical commit as proof, and a router decision that closes a whole batch.  Both are
**router-only and legacy-only** — nothing on the normal `standard`/`high` path changes.

**Tier `none`.** `review_tier` accepts a third value, `none`: the item is not reviewable,
so no reviewer's verdict can ever move it to `done`.  Because that removes the gate rather
than raising it, the store admits it only under its own precondition — the definition must
be decision-only, `evidence_kinds == ['decision']`.  Any other combination (including
`['any']` and a decision kind alongside another) is refused `validation` at
`create_items`/`import_items`/`edit_definition`, and only the router may name it at all: a
non-router `none` is `forbidden`.  `default_review_tier` never produces `none`, so an
omission still means `standard`/`high`.

**`close_on_decision(principal, key, batch_ref)`** (router, `store_close.py`) takes a
`claimed`/`in_review` tier-`none` item straight to `done`.  It requires, all verified in
read-only I/O *before* `BEGIN IMMEDIATE` — and the batch digest, decisions-file checks
and `review_tier=='none'` are re-verified *inside* the write lock (X7-FIX-B), so a file,
row or tier swapped between the check and the commit is refused:

* at least one attached `decision` evidence for the current epoch, and **every** attached
  decision must verify (the `decision` verifier *and* the configured decisions file);
* `batch_ref` shaped `"<path>@<sha256 hex>"`.  The keeper resolves the path and refuses
  `forbidden` unless it lands inside one of the config's `batch_roots` directories — the
  containment test runs on `realpath`, so `..` segments and a symlink planted inside
  `batch_roots` that points out of them are both refused.  It then hashes the bytes and
  refuses `evidence_unverified` on any mismatch, and the same way on an unreadable or
  missing file.  No `batch_roots` (the default) means the verb can never succeed.
* the `Closes:` rule, the same whole-token shape as `Waives:` (X6c3): if any of those
  decision rows carries a `Closes: <project:ID>` token, `key` must be one of the named
  items (`evidence_unverified` otherwise; a row may name several).  A decision with no
  `Closes:` token at all is still a valid close: the hashed batch file is the authorisation.

Inside the write lock the verb re-reads the row and re-checks state, epoch and the exact
set of decision-evidence ids, so a decision attached or replaced while the I/O was running
loses the race (`conflict`).  It then writes the cache row (`done`, `reviewer` = the router)
*before* appending `closed_on_decision {decision_ids, batch_ref, sha256}` — the event
is what locks the key, exactly like `review_passed`, and every trigger, the `fold` and
`verify_integrity` treat the two the same.  Afterwards `edit_definition`, `cancel`,
`add_evidence` and a second close are all refused (`locked`), at the SQL level too.
Refusals: non-router (`forbidden`), an item still `open` or already `done`/`cancelled`
(`bad_state`/`locked` — an `open` legacy row must first be moved into an active state by
`set_legacy_state`, a later task), a tier that is not `none` (`bad_state`).

**Commit-gated decision closes.** When a tier-`none` item defines `evidence_kinds` as `["decision", "commit"]` (order-insensitive), `close_on_decision` additionally requires at least one verified `commit` evidence row attached to the current epoch whose sha is an ancestor of the project's configured target ref (`main` by default, or the configured `ref` in `repos`). The commit is re-verified at close time under the write lock. If no commit evidence is attached, the close refuses `evidence_unverified` ("no commit evidence"); if the commit exists but is not an ancestor of the configured ref, it refuses `evidence_unverified` ("commit <sha> not merged into <ref>"). When the decision carries a `Closes: <key>@<sha>` token, each verified commit evidence sha is resolved to its full 40-hex id in the repo and the token must be a case-insensitive prefix of a resolved id (7..40 hex), refusing `evidence_unverified` otherwise; conflicting Closes tokens for the same key are refused. Items with `evidence_kinds == ["decision"]` close on the decision alone, exactly as before. The router may attach `kind="commit"` evidence using `add_evidence_router` with `ref={"sha": ...}` while the item is `in_review`.

Historical seat waiver rows use the decisions-log header
`id | project | decision | by | why / evidence`. For example:

```text
| D-6000 | demo | Waiver: merged-without-witnesses; Waives-seats: demo:A demo:B; Cutoff: 2026-11-14; Date: 2026-11-14 | operator | historical review exception |
```

The `by` cell must contain the case-insensitive token `operator`, separated from
other tokens by commas or plus signs. `Waives-seats:` lists exact, space-separated
item keys. Optional `Cutoff:` in the decision cell replaces rule 3.37's cutoff
for those keys only. It accepts an ISO UTC date or datetime (`Z` or `+00:00`),
must occur once, and cannot be later than the waiver row's own date. Malformed
markers such as `Cutoff:2026-11-14` refuse as `historical_cutoff` instead of falling
back to rule 3.37. Dates come
from a `date` column or a unique `Date:` field in the decision cell; missing or
ambiguous dates refuse. The GO date and merge timestamp must precede the cutoff.
Without `Cutoff:`, rule 3.37 applies (default 2026-11-13 UTC). Auth/secrets risk
tags always refuse a seat waiver. A GO scope refusal names the item and decision.

**Evidence kind `commit`** is the historical twin of `commit+ci`: it passes on "this sha
exists and is an ancestor of `main` in the project's registered repo" and deliberately drops
the trailer, claim-window and CI checks a migrated item can no longer satisfy.  That makes
it a weaker proof, so it is attachable **only by the router through
`add_evidence_router`**; a claimant or delegate naming kind `commit` is refused `forbidden`
and stores nothing.  Unknown project (no `repos` entry), missing repo and any git error all
fail closed as unverified. Merged task context names the introducing merge on the target ref’s first-parent chain. Nested batch parts name the batch PR, and PR branches refreshed from main retain their landing PR. A two-parent landing commit that carries its own `Plan-Item` trailers supplies merge evidence even at the tip. The keeper takes the PR number from the subject’s final `(#N)` when present and binds the PR head to the second parent. Failed git walks leave the merge unknown.
Origin-main `decision_ref` closes need no batch file; exactly `[decision]` evidence and tier `none` are required for every item kind; stored reviewed decision-only items close through review, while create/edit requires tier `none` and router authorization for a new decision-only declaration (also on an existing `none` item); router imports preserve stored reviewed decision-only definitions with a warning count, and unrelated edits preserve them; see [decision-close proof, usage and refusals](keeper/keeper-decision-close.md), also covering router-only `close_merged` for merged reviews and dry-run `--from-open` closes on head-bound GO decisions (D-5504/D-5506): timestamped (`(HH:MMZ)` or `(HH:MxZ)`, optionally `~` before HH and/or `, date -u` before `)`) `GO #PR ... GO-HEAD <sha40>` rows (including punctuated/backticked PR numbers) and recorded cross-writer-family seats (D-5529), with automatic backfill from cited witnessed mirrored reviews when families are omitted and opt-in historical `--waive-seats` requiring OPERATOR authority with a whole-token `Waives-seats:` list and rule 3.37 cutoff (default 2026-11-13 UTC), with auth/secrets cards always requiring two non-Anthropic families and refusing seat waivers; waived evidence records review tier, risk tags and the decision id overriding the seat rule, and invalid stored card tags refuse by name. Skipped legs and quota refusals can precede one completed witnessed attempt on the head. The record must carry a successful finish reason, and an earlier non-quota failure invalidates the fallback chain (D-5924; [details](review-quota-fallthrough.md)).

The landing journal saves the prepared mode and receipt inputs with the train head. A resumed tested fast-forward writes its receipt and telemetry from that manifest and requires the explicit seat-policy acknowledgement before closing items. See [Keeper settlement](keeper-postgres.md#schema-and-migrations).

A train GO row with `GO-KEYS [project:A,project:B]` scopes `close-merged` to the listed full keys. The final delivery needs its own GO. Earlier partial deliveries need to be on its default-branch history.
Legacy rows may also vouch for a key through a named part head inside their GO-HEAD. That head is recorded as `go_part`.
The delivery index excludes reverted work and drop commits. Reverting a revert or delivering the work again can restore the delivery evidence.
Deploy evidence uses the part diffs that carry the key. Without an item-level `Deploy-Hosts:` declaration, the part's `Deploy:` trailers supply hosts for its shipped units. A key carried only by the aggregate train merge uses its whole diff. A declined close prints `REFUSED` with the reason.

For a timer-driven service, report `type: oneshot`, `result: success` and `exec_main_exit_timestamp` from `LC_ALL=C systemctl --user --utc show -p Type -p Result -p ExecMainExitTimestamp UNIT`. Keeper compares the exit timestamp with the latest first-parent change to the service or timer on the configured default ref. Closing also requires verified `is_active: active` evidence for the same-name `.timer` on the same host.

A merge into main closes its items with no router call (`close_merged_auto`, role `system` or `router`; the Rust job `plangraph merge-close` calls it every 10 minutes from `deploy/systemd/pg-merge-close.timer`, and the batch land step calls the same job). It reads the default ref once per repository and takes every open, claimed or in-review item a `Plan-Item` trailer names (a `Plan-Item-Part` trailer names none), proves the merge with the same rules as `close_merged` (merged PR head or direct commit, no open lane, `Merge-Repo:` honoured) but needs no GO row, no `--from-open` and no decision id, and records `commit` evidence `{source: merge-close}`, clears claim, delegate and lease with the closing `review_passed {released: true}` and sets `done`. The keeper setting `require_seats` in `keeper.json` (default off; the job environment `PG_MERGE_CLOSE_REQUIRE_SEATS` or a request field `require_seats` overrides it for that call) decides the witnessed seat proof that `close_merged` without `--family` and this close ask; tier `none` needs none. With the setting off a missing, unavailable or unwitnessed seat does not refuse: the close proceeds on the merge evidence and stores the flag `seats_missing` (`{run_ids, reason}`) in the `review_passed` event and the close evidence, the reply carries `flags` (on the `closed` or `would_close` row of the key, not in `listed`; `plangraph merge-close` prints it as a last `FLAGGED seats_missing: <run ids>` column), and `close-merged` prints `FLAGGED` instead of `NO-WITNESS`; an auth/secrets card and every other refusal stay refusals. The reply lists `closed`, `would_close` (`apply: false`), `listed` (every merged key it refused, with `code: reason`; a key closed with a flag is not in it; an auth/secrets card is always listed and left to the router) and `merged_not_done`; `plangraph merge-close --count` prints that number alone.

`plangraph reconcile-merged --dry-run --state PATH` previews merged work once
per hour through the user timer. The job atomically replaces the watcher state
file and emits one JSON batch line to the service journal for routing. Each
batch contains the merged-open count, refusal counts, and proposed actions.
The Keeper raises `merged_open_reconcile` on the second consecutive nonzero
pass and clears it after a zero pass. Routing applies any proposed action
under its own GO decision.


The writer families a seat may not share (`close_merged`, the automatic merge close and the `claude-seat` verifier) are the recorded writers: commit evidence, the `Writer` trailers of the commits that landed the item's `Plan-Item` work, the item's `owner_family`, `writer_model` and the writers of recorded runs. The claimant and delegate principals (an orchestrator such as an L1) count only when no writer is on record at all, so a claude-seat on a diff written by another family closes, a claude-written diff stays refused (`claude_seat_writer_family`) and an unrecorded writer behind a Claude claimant stays refused. `claude-seat` evidence for any merged head of the item counts as that part's seat (`claude_seat_head` only when the head is no merged head of the item), and a high-tier item closes on one claude-seat plus one seat of another family.

`close_merged_auto` fetches and indexes the scanned repositories before its request takes the keeper lock (`warm_merge_scan`, only for a system or router token) and then runs frozen: it reads only the warm index and the warm hosted-lane answers (a project that is not warm is listed `merge_index_not_warm`, a lane whose answer is missing `unmerged_work_or_lanes_unknown`), so git and the forge are never asked under the lock; a direct store call is not frozen. The automatic close looks for `Plan-Item` commits in the scanned repositories and refuses `merge_repo_ambiguous` if several match. The router GO close uses the item's repository by default. A `Merge-Repo:` hint selects the repository for either close.

Computed [train delta admission](landing-train.md) reads the keeper mirror and requires an authenticated serving attempt bound to the reviewed artifact and a completed response.

A landed batch closes its part items and its batch item in one step: the batch merge commit carries one `Plan-Item` trailer per part item and the batch item, and one `Review-Run: <project:ID> <run-id> <part head>` line per keeper-bound part review. `close_merged` and the automatic merge close read those lines of the merge that landed the item's head: a part head counts as the seat head when it is reachable from that merge's second parent (outside its first-parent history) and a commit of its range carries the item's `Plan-Item` trailer; a merged head without a citation, or one the cited parts do not cover (another commit of the item in the same batch), keeps needing its own seat, and the cited record is still verified by the witness adapter (served model, exact part head, passing verdict, binds this item), so a part without a recorded seat run still refuses `NO-WITNESS`. A merge commit on the first-parent chain that carries the trailer itself is its own introducing merge (its head is its second parent), also after later merges.

`close_merged` binds a GO row to the PR it names before the trailer search: a row that starts with `GO` (or one numbered ruling of a `RULINGS` row) naming one PR (`#N`, `PR N`) and one full `GO-HEAD` is accepted when that PR merged on the default branch with exactly that head and the row names the item; the PR number of a merge-commit subject is its trailing `(#N)` (the form `merge_batch land` writes), else its first `#N`; when the subject carries no `#N`, `close_merged` also takes the PR number from the GO row of the `D-id` when that row names `PR N` and a head `H` introduced on the merge's second parent (an ancestor of the merge commit); an item names the repository of its merge with a `Merge-Repo: <project>` line in its description or note. With an alternate repository selected, the GO row can prove the named PR without a `Plan-Item` commit. A refusal names the head the row asked for and the merged heads it found. `close-merged <key> <D-W> --waive-seats <D-W>` (the same id twice) accepts the OPERATOR row `D-W` itself as the decision, no GO row and no per-commit PR number: the row starts with `Waiver: merged-without-witnesses`, lists the key in `Waives-seats:`, every `Plan-Item` commit of the item is on the default branch with its merge (or direct commit) dated before the row's cutoff (rule 3.37, default 2026-11-13) and no open lane remains; it is a dry run (`would_close`) until `--apply`, and the evidence keeps the waiver id, the merged heads and the PR numbers (null for a direct commit). `close_merged` reads the decisions log once per phase from a snapshot: a strict (authorization) parse is served only from a strict read, never from a best-effort fallback. The introducing merge is found by walking the whole first-parent chain of main (no merge-count window). Per merged head the dated GO row wins over an undated one, and a scanned row replaces the supplied decision id only when it is a later one. The locked phase recomputes the seat waiver from a fresh snapshot and refuses `conflict: merged_seat_waiver_changed` when it differs from the pre-lock read. An item whose merged work (each Plan-Item commit's own diff against its first parent plus what the merge that brought it in landed, so an untagged follow-up commit of the same PR counts) touches `deploy/systemd` (`.service`/`.timer`/`.socket`/`.path`) or adds a host binary (an added `rust/<crate>/src/main.rs` or `src/bin/*.rs`; editing or deleting an existing one does not) is not closed (by `close_merged`, and by the automatic merge close in preview and apply, which lists the item as `deploy_pending`; the merge a `Merge-Repo:` line names counts like a Plan-Item merge, read at the commit that landed on the default branch, so a squash merge counts; the close pins the Plan-Item history, merge proofs and deploy diffs to one default commit. The automatic close warms deploy diffs, including the named merge, outside the request lock. An unrelated first-parent fast-forward preserves the scan. A rewritten ref or new tagged work requires a fresh scan. A frozen run lists `evidence_unverified: deploy_scan_not_warm` when its snapshot is missing or stale, and `deploy_scan_unreadable` when the scan failed, including a failed history read. The write transaction checks the live ref again and refuses `conflict: merged_on_default_changed` if it can no longer validate the scan) until every host named in its `Deploy-Hosts: a, b` line has a verified `deploy` evidence (`host`, `unit`, `is_active`: `active`, or `installed` for `bin:<name>`; the router may attach it to open items): `close_merged` otherwise leaves the item open, answers `closed: false, reason: deploy_pending` with the missing `host:unit` pairs and lists the item in `alerts` as a `deploy_pending` finding.
The router may add `Merge-Repo: <project> <commit SHA or #PR>` to an open or
in-review item's note. Keeper verifies the named merge on the configured
repository's default branch and binds it to the item's GO row. The close audit
records `merged_on_default` or the refusal reason, including
`merge_repo_pr_not_merged` for an open PR or a closed PR without a merge.
Item context reports
`closed_pr_lanes` when a closed PR's hosted head still matches the lane branch.
These lanes no longer block a merged close.

## API (stdlib `http.server`, threaded; one Store with a lock; bind 127.0.0.1 by default, `--host`)

A router can send the keyless body `{"dry_run": true}` to `POST /v1/close_merged` as a deploy probe.
A permitted probe returns `{"dry_run": true, "route": "close_merged"}` with
zero item, audit, evidence, or event writes.
To preview a particular close, include the item's `key` and `decision_id` with
`dry_run: true`. The keeper checks the close and returns the proposed transition.
`POST /v1/<verb>` JSON body → JSON; `GET /v1/{ready,show,blocked,waves,critical_path,percent,alerts,digest,snapshot,whoami,health}` plus
`GET /v1/audit-meta?project=&stale_days=` (router/L1 only), `GET /v1/changelog?since=` and the [principal-scoped event replay route](contract/keeper-contract-v1-routes.md#get-v1events).
The shadow collector also reads `GET /v1/messages?since=SEQ` for bus messages and
`GET /v1/hosts` for current host levels.
The spawner's `record_run` call can include `native_launch_refusal` for a finished run with a nonzero exit. Keeper publishes a `native_launch_refused` finding when it records that run for the first time.
An automatic merge close records the terminal verdict for the claimant and
delegate whose claim it released.
`Authorization: Bearer <token>` (health is open). Errors: `{"error": code, "msg": …}` with 4xx. Compact text responses where the verb is
meant for agents (`ready`, `show`, `alerts`) via `?fmt=text` (default text for CLI). The server also runs the reaper every 60 s and rewrites
`plan.snapshot.json` (path from config) after every committed write. Config file (JSON, mode 600 checked): db path, snapshot path, `merge_scan` (optional list of `repos` projects the merged-on-default scan reads; default every repo; `plangraph merge-close --repos` prints the scanned repositories), repos
{project: path} or {project: {path, ref, non_pr: true}}: the router-owned boolean `non_pr: true` opts into merge-commit-on-target-ref proof instead of a hosted PR association (for non-PR repositories); direct pushes remain refused, green CI is required (`ci: none` cannot use this proof), and principal, trailer, time and floor guards remain. Also runs_dir, decisions_file, ci per project (plus `forgejo_url` + `forgejo_token_file`
for `forgejo:` values), bind, and `default_lease` (7200), `lease_warn_s` (900), `review_overdue_s` (14400), `alert_window_s` (604800; `alerts` lists findings and repeated refusals only for open, claimed or in_review items and only inside this window; the JSON `history` holds the counts left out, no row is deleted; the `alerts` table holds them, written when an alert is raised or cleared with one `alert_raised` / `alert_cleared` telemetry row in `alert_log`, so the call never scans `audit_log`; a refusal row remembers each refusal's time, so one older than the window stops counting on its own (at most 256 are kept per key); the daemon runs a one-time idempotent backfill from history at start, its marker and rows in one transaction, merging into an alert row the table already holds for the same key and code instead of adding a second one), each a JSON integer from 1 to 315360000 seconds. Applied to the daemon's Store at start; restart to change. Secret config values are never printed.

## CLI `pg`
`pg ready [--project P] [--all] [-n 10]`, `show KEY`, `claim KEY [--lease 2h]`, `delegate KEY WORKER`, `heartbeat KEY`, `release KEY`,
`evidence KEY --kind K --commit SHA | --run ID | --task N | --decision D-NNN`, `submit KEY`, `verdict KEY --pass|--changes "why"`,
`waive KEY D-NNN`, `cancel KEY`, `priority KEY N` (integer 0–10: 10 most important, 0 optional; define, import, edit and batch writes refuse other values with `validation`), `define …` (a new or created lane needs a non-blank `done_criteria`, refused with `done_criteria_missing` for every principal, and an edit may not empty it; legacy imports and `audit-meta` are unchanged; all new items, including decisions, require non-blank `Why:` and `How:` sections; missing sections report `missing_why_how`. MCP create and CLI define defer decision checks to storage, including `apply_ops`, after duplicate-key, conflict and locked refusals; legacy imports and existing-item edits remain exempt. `pg check` warns on open items without), `blocked`, `waves`, `critical`, `percent`, `alerts`, `digest`, `snapshot`, `whoami`,
`render --out F [--cdn]`, `audit-meta [--project P] [--stale-days N] [--skeleton DIR]`; admin (local, on the keeper host): `pg admin init|add-principal|revoke|verify|import DIR|export DIR`.
Env `PG_URL`, `PG_TOKEN_FILE` (path of a mode-600 file; never pass a token on argv). Default output = the smallest useful text; `--json` for full.
Exit codes: 0 ok, 2 refused (stderr `code: msg`), 3 unreachable (CLI then reads the local `plan.snapshot.json` read-only for read verbs and says so).

If `claim`, `claim-batch`, `heartbeat`, `heartbeat-batch`, or `priority` gives an unknown outcome, repeat the same request with the printed `PG_IDEMPOTENCY_KEY` and the same credential. The keeper replies from the receipt written in the mutation's transaction. Run-token heartbeat outbox entries carry that key. The journal's `request_timing` records include `verb`, `wait_ms`, `hold_ms`, `query_ms`, `snapshot_write_ms`, and `total_ms`. Every `show`, claim, heartbeat, and `set_priority` request contributes a latency sample. Snapshot builds read the store under its lock, then encode, sign, and publish outside that lock. A review-overdue finding moves to alert history when the item leaves its review round.

Use `pg claim-batch KEY...` to claim up to 200 distinct keys with a shared lease and optional audited `--force-stale` reason. Use `pg heartbeat-batch --claim KEY EPOCH --claim KEY EPOCH` to renew held claims. The corresponding HTTP endpoints are `POST /v1/claim_batch` with `{"keys":[...]}` and `POST /v1/heartbeat_batch` with `{"claims":[{"key":"project:ID","epoch":1}]}`. Each batch commits in one transaction and returns a `results` array in request order. Each row contains the key and either its epoch and lease deadline or an error and message. The CLI reports every row and exits 2 if any key is refused.

The keeper setting `claim_preflight_s` defaults to 30 seconds and accepts a positive
JSON integer. The daemon applies it at startup to claim, worker claim, and claim
batch before writing. Overlapping claims share in-flight authority lookups and
can wait up to the earlier of the leader's deadline and their own deadline. A
lookup error is shared with waiting claims. A deadline refusal names the
preflight step that ran out of time. Independent checkouts refresh in parallel,
with up to four workers per scan. A later claim refreshes repository authority
again, even when a previous work index is recent.

`pg define` and `pg define --edit` refuse review clauses in `done_criteria`
or `description` that pin a model or leg from the routing catalog. Ask for
“a review seat outside every writer family” so the spawner can choose the seat.
Phrases such as “approved by” and labels such as `Reviewer:` or `Seat=`
can pin a reviewer. A request to review the output produced by a named model
can still name that model.
`pg check` lists existing nonterminal items with model pins as
`NAMED-REVIEW-MODEL`. The JSON output includes `named_review_models`, and
`--strict` exits 1 when it finds a pin.

When a description edit introduces a newer plan version or explicitly
supersedes a rule still cited in the title or done criteria, the edit response
returns both fields and a reminder to update them together. The keeper
includes that finding in the stale reason on `show` and applies the existing
audited stale-claim override procedure.

An edit that changes `done_criteria` appends a row to `criteria_amendments`
in the same transaction as the definition event. The row links to that event
and records the before and after text, editing principal and time. Supply
`criteria_reason` and `criteria_evidence` in the edit fields to record the
rationale and supporting evidence. With those fields omitted, the row records
that the edit came through Keeper and links to the definition event.

A definition whose How asks the writer to use a design or contract needs a
`design_ref`. Set it to a repository-relative path or use
`REPOSITORY:PATH` for a file in another repository. Show, short briefs
and snapshots expose the stored reference. The native batch-size input accepts
`design_ref` on each item and `design_roots` indexed by repository qualifier.
The generated brief embeds the selected files verbatim and reports the
reference if a file cannot be read. If a plangraph lane's How names external
code, it must also name the plangraph implementation target.

Keeper records slice ancestry from the replanner's definition. A definition
attached under a slice is refused with the parent key, including after an
edit to the parent's criteria.

Ready and claim include qualified Plan-Item matches from other repositories
in the configured merge scan. A bare Plan-Item ID matches within its own
project, and a merge message can carry a Plan-Item line in its body. Merged
work can appear in `needs_reconcile` instead of the ready list.

The reaper raises `gate_dependency_capacity` when a nonterminal gate or goal
has at least 190 non-cancelled dependencies, including done dependencies.
Define counts unfinished dependencies against the 200-dependency limit.
When an attachment would exceed the limit, the refusal names an available
split gate when one exists.

## Tests (minimum; write first)
state machine: every allowed edge + every disallowed edge refused · claim race: 3 threads → exactly one winner · stale epoch refused ·
lease reaper · attack suite at store level: edit/delete/reopen/re-point a DONE item, `corrects` pointing at non-done, evidence/event/edge
insert for DONE, wrong role, other project, reviewer==owner, same family, unknown family, forged actor field in a request (ignored) ·
attack suite at SQL level through `open_db`: UPDATE/DELETE/INSERT/INSERT OR REPLACE on DONE rows → abort; DROP TRIGGER / writable_schema /
ALTER → denied by authorizer · hash chain: tamper a payload with a raw sqlite3 connection → `verify_chain` reports it · cache vs fold
mismatch → `verify_integrity` reports it · each verifier with fakes (pass + every failure) · import → export → diff identical on tests/fixtures
and on data/plan-v3 · API: auth required, refused → 4xx with code, health open · CLI against a live in-process API · MCP: `tools/list`,
`tools/call` round trip over pipes.

## Residual risks
The triggers and the authorizer guard *this process's* writes, not the file. A raw-SQL writer — the keeper OS user, or root — can `INSERT` a forged
`review_passed` for a key that never left `claimed`, and the fold, the cache and `verify_integrity` all agree with whatever rows are in the DB, so the
item reads as legitimately done. Only the hash-chain head anchor (M2b, an external/notary copy of the head) detects such a row. Treat the DB file's
unix permissions and the host as part of the trust base.

Evidence inside `apply_ops` (B1 seat finding): the evidence verifiers are NOT re-run inside the transaction. A result computed before the lock is bound to the item's
state and epoch, which are re-checked in the lock (`conflict` otherwise), and `close_on_decision` re-checks the decision-evidence id set; but a verifier's own external
input (a git/CI/HTTP answer, a decisions file edited after the pre-lock read) is only as fresh as the pre-lock call. Evidence for an item an earlier op of the same
batch creates or moves is verified in the lock instead.

Waiver rule (D-217 / X6c3): `waive_second_family(key, D-NNN)` requires an exact ASCII `D-[0-9]+` identifier (`validation` otherwise) and succeeds only if that row in the decisions file contains `Waives: <project:ID>`
for exactly that key (whole token; a row may name several keys; code blocks do not count). `Waives:` and `Closes:` tokens count only inside the DECISION cell (3rd column) of their D-row (D-276). A forged D-id still needs the decision verifier, so the
residual risk above applies to waivers too: a raw-SQL writer who can edit DECISIONS.md can name any key. Malformed stored waiver values waive neither the second-family gate nor the verdict count.

Waiver re-check window (S1-2, documented residual): the `Waives:` row is verified before the write lock is taken, as the evidence verifiers are, because
the decisions-file read is pre-lock I/O. If DECISIONS.md is edited between that read and the write, the waiver is recorded against the earlier text.
The in-lock state/epoch/round checks still apply; only the decisions-file content is not re-read under the lock.

**`apply_ops(principal, ops, batch_ref=None)`** (router, `store_ops.py`; HTTP `POST
/v1/apply_ops`, MCP tool `apply_ops`) applies a list of BATCH.md ops (`create`,
`edit`, `legacy_state`, `add_evidence`, `close_on_decision`, `cancel`, `priority`,
`rehome`, `set_kind`, `add_dep`, `remove_dep`) in one
`BEGIN IMMEDIATE`. Each op runs its single verb's inner function (same auth and
state checks) with the whole-graph validation deferred: it runs once over the
final graph against the graph at lock time. Any refusal or problem rolls back
items, edges, events and evidence together (`Refused` message
`op <n> <op> <key>: <code> <message>`, `n` 1-based), so the hash chain gets all
ops or none. Evidence for a `claimed`/`in_review` item that no earlier op moves is
verified before the lock and its state/epoch re-checked in the lock (`conflict`
otherwise); evidence for an item an earlier op creates or moves is verified in
the lock, and `close_on_decision` re-reads its batch and decision files in the
lock as it always did.

`add_dep {op, key, dep}` / `remove_dep {op, key, dep}` (PG-LE-DEPS-OPS) change ONE entry of `key`'s
`depends_on`: they read the item's current edges inside the transaction (later ops see earlier ones) and delegate to the very same `edit_definition` write, so auth, `open`-state, cycle and unknown-dependency validation and the `item_defined` event are the edit's. Explicit dependency lists allow up to 200 distinct open targets. Named edits can repair a legacy overage without adding open targets. A dep `add_dep` already has or `remove_dep` lacks writes nothing, appends no event and answers `{"key", "warnings": [...]}`; a dep naming an item whose state is `cancelled` is dropped from the resulting list with `warnings ["dropped cancelled dependency <key>"]` (a full `edit` of `depends_on` drops it the same way, so `views.validate`'s `depends_on_cancelled` no longer refuses the write), an unknown dep key is still `validation`. The result keeps `{"key": ...}` alone unless something was dropped or skipped.

**Credential-shape guard (D-374, `credential_guard.py`).** `create_items`/`edit_definition`/`import_items`/`apply_ops` refuse (`validation`, naming the field,
never echoing the value) a `title`/`note`/`done_criteria`/`description` (or any other folded field; `description` is its own column, cap 2000) in which a `word/secretish` token (`\w+/\w{6,}`) sits within 120
characters of `credential|password|passwd|pwd|login`; there are no exemptions, so rephrase paths and URLs near credential words.
`pg render` masks any match that is already stored as `[redacted]` and prints `render: masked N credential-shaped string(s)` on stderr.

## Signed snapshot (M2B-SIGNED-SNAPSHOT, D-789)

Optional keeper config keys: `signing_key` (ed25519 private key, PEM, mode 600, on the keeper host only) and
`snapshot_pubkey` (its public key, for readers). With `signing_key` set the keeper writes `plan.snapshot.json.sig`
(base64 ed25519 signature over `b"plangraph-snapshot-v1\n"` + the exact snapshot bytes) before each snapshot; a reader
with `snapshot_pubkey` (the CLI's local fallback, `pg admin verify-snapshot`) refuses an unsigned, tampered or
foreign-key snapshot. Keys: `pg admin snapshot-keygen --private P --public Q` (refuses to overwrite; never prints keys).
Dependency: `cryptography >= 41.0.7` (Apache-2.0 OR BSD-3-Clause; Ubuntu `python3-cryptography`), imported only when
signing or verifying is configured. Going live (key generation + config) is a parked operator/router step.

### Going live with the signed snapshot (operator step Q-PG-SNAPSHOT-KEYGEN)

On the keeper host, as the keeper's user (keys never leave the host; nothing is printed):
```
cd ~/code/plangraph/.claude/worktrees/proposal
PG_CONFIG=~/.local/share/plangraph/keeper.json PYTHONPATH=src python3 -m plangraph.keeper.cli admin snapshot-keygen \
    --private ~/.config/plangraph/snapshot-signing.key --public ~/.config/plangraph/snapshot-signing.pub
# keeper.json: add  "signing_key": "~/.config/plangraph/snapshot-signing.key",
#                   "snapshot_pubkey": "~/.config/plangraph/snapshot-signing.pub"
systemctl --user restart plangraph-keeper.service
PG_CONFIG=~/.local/share/plangraph/keeper.json PYTHONPATH=src python3 -m plangraph.keeper.cli admin verify-snapshot
```
Readers on other hosts get only the `.pub` file and `snapshot_pubkey` in their config. The keeper unit itself is
`deploy/systemd/plangraph-keeper.service` (persistent user unit, enabled; Linger=yes).

## Local plan copies on other hosts (PG-LOCAL-COPY, D-818)

Other hosts never run a keeper: they keep a verified LOCAL COPY of the signed snapshot, pulled before
and after every update and every 5 minutes (`deploy/systemd/pg-snapshot-pull.{service,timer}`).
Writes still go to the keeper through the host's existing channel. Run-token heartbeat, evidence,
and submit writes use the [durable outbox](#run-token-write-receipts).

- Keeper side: `python3 -m plangraph.keeper.snapshot_serve --config keeper.json` is the forced
  command of a `restrict` authorized_keys line (one pull key per host). It prints one JSON line
  `{"format": "plangraph-snapshot-pull-v1", "snapshot": <base64>, "sig": <base64>}` and nothing
  else, ignores `SSH_ORIGINAL_COMMAND` and stdin, opens files with `O_NOFOLLOW`, and serves nothing
  unless signing is configured and the snapshot verifies. Pinned checkout: `~/.local/share/plangraph/snapshot-serve` (outside `.claude/worktrees/`, live-sync refresh only); read-only helper: `docs/SNAPSHOT-SERVE-PATH.md`.
- Reader side: `pg --config reader.json sync pull` runs a locked-down ssh (`-F none`, BatchMode,
  StrictHostKeyChecking, no agent/X11/forwarding/tty, host after `--`, every config value validated),
  verifies the signature with `snapshot_pubkey`, refuses a copy older than the newest one seen
  (`head_seq`, kept in `<snapshot>.hwm`) or a different head at the same `head_seq`, then installs
  high-water mark, signature, snapshot (temp + fsync + rename each; one pull at a time via
  `<snapshot>.lock`). Exit 0 installed, 1 refused, 3 unreachable (copy unchanged). Reads with the
  keeper unreachable use this copy (`pg` exit-3 fallback, signature checked).
- Snapshots carry `head_seq` (the last event's seq) for the rollback check. Config paths may use `~`.
- Reader config (mode 600): `{"snapshot": "~/.local/share/plangraph/plan.snapshot.json",
  "snapshot_pubkey": "~/.config/plangraph/snapshot-signing.pub", "pull": {"host": "<keeper address>",
  "user": "s", "identity": "~/.ssh/pg-snapshot-pull", "port": 22}}`. The keeper's ssh host key must
  already be in the reader's `~/.ssh/known_hosts` under that address (no trust on first use).
- Operator steps: the data repo `docs/proposals/2026-10-09-d818-local-copy.md`.
- Hosts without a timer (e.g. a hypervisor host that runs no daemons/timers) pull ON DEMAND; the L1 there does:
  1. at the START of every plan-touching task: `PG_CONFIG=~/.config/plangraph/reader.json PYTHONPATH=src python3 -m plangraph.keeper.cli sync pull`
  2. AGAIN right after its plan update (claim, evidence, submit, edit, batch) went through, so the local copy shows the keeper's result.

## Hash-chain anchor (M2B-ANCHOR, D-789 D1)

The keeper writes the chain head `{"v": 1, "seq", "head", "ts", "reason"}` to an append-only anchor log outside the
DB: at start (`start`), within one reap interval of every done transition (`done`: review_passed,
closed_on_decision, answered) and hourly while the head moves (`hourly`). At start it REFUSES a DB whose head_seq
is behind the newest anchor (rolled-back file) or whose event at the anchored seq has another hash (fork):
`keeper: REFUSED by the anchor log: …`, exit 1. Restoring an older `keeper.db` backup is therefore refused once
newer anchors exist; that is the point. An anchor write that fails is logged once and retried on the next tick;
it never stops the keeper.

- Production (`anchor_socket` + `anchor_log`): a second user `pganchor` owns `/var/lib/pganchor/anchor.log`. The
  keeper connects to `/run/pganchor/anchor.sock` (`deploy/pganchor/pganchor.socket`, Accept=yes, group of the
  keeper's user, 0660). Each connection starts `pganchor@.service` (User=pganchor, sandboxed, no network), which
  runs a copy of `src/plangraph/keeper/anchord.py` (stdlib only, one file) and appends one record unless it is
  older than the last one or forks it. The keeper cannot rewrite or truncate the log.
- Interim (`anchor_file`): the keeper appends itself, detection only; the operator sets `chattr +a` on the file.
- `pg admin verify-anchor`: every record against the chain (`anchor_mismatch`, `anchor_ahead`, `anchor_order`,
  `anchor_malformed`), the start check, and the interim file's append-only flag.
- Daily tag: `tools/anchor_tag.py --repo ~/code/<data-repo> --anchor-log <log> [--push]` tags the data repo
  `anchor-YYYYMMDD` with the newest record (scheduling it is an operator/L1 step).
- Operator install steps: the data repo `docs/proposals/2026-10-09-m2b-anchor-install.md`.

## Withdraw from review (PG-WITHDRAW-REVIEW, D-867)

`withdraw_review(key, decision_id)` (router only; `pg withdraw KEY D-NNN`, MCP `withdraw_review`) moves an
`in_review` item back to `claimed` (same epoch, fresh default lease, reviewer cleared) with one hash-chained
`review_withdrawn` event and revokes the item's live run tokens. The decision must name the key in `Closes:`, and
the item must be decision-closable: its evidence kinds include `decision`, or its done-criteria ask for no
commit/merge/CI/seats. It never records a verdict; the claimant then attaches decision evidence and the router
closes it on the decision.

## Reopen a review (PG-REOPEN)

`reopen(key, reason, decision_id)` (router only; `pg reopen KEY D-NNN --reason TEXT`, MCP `reopen`) moves an
`in_review` item back to `claimed` for the same claimant at a fresh work epoch (+1, round reset, reviewer and
waiver cleared, fresh default lease) with one hash-chained `item_reopened` event and revokes the item's live run
tokens. The decision only has to be recorded - no `Closes:` naming - and the reason (non-empty, <= 500 chars) is
carried by the event. It never closes an item and never records a verdict; evidence, rounds and verdicts of the
superseded epoch no longer count, so the claimant attaches what the new epoch relies on again.

## Operator question choices (QA-IMPORT, D-865)

question / decision / operator_task items may carry `choices` = `{options: [{id, label, consequence}], default,
deadline, blocks}` (<= 8 options, ids `[a-z0-9-]`, label <= 60, consequence <= 160, deadline `YYYY-MM-DD`, text
credential-guarded, no control characters); create/edit set it, `show`, the snapshot and `needs_you` return it.
md-sync reads it from the end of a QUESTIONS.md "Exact step" cell:
`Options: enable = mail goes out as your Gmail; own sender = needs a new mailbox; *stay disabled = nothing changes. Deadline: 2026-10-12`
(`*` = default, `;` separates; an option whose id is not the slug of its label is written `label {id}`
with `id = [a-z0-9][a-z0-9-]{0,39}`, e.g. `Keep the Shelfmark lane {keep} = stays`; a malformed block,
including a malformed `{...}`, stays plain text). Rows whose ID cell is not exactly one id are
listed in the md-sync status file under `unmatched`. In the decisions log, `Closes:` and `Waives:` match whole keys after stripping surrounding prose punctuation. Quoted keyword examples and prose after a list supply no keys. For a router row with a `Closes:` keyword and no valid key, md-sync reports the invalid row once per decisions log using `plangraph/md-sync-seen.sqlite` under `$XDG_STATE_HOME` (default `~/.local/state`).
QUESTIONS.md row format `| Prio | ID | Kind | Parent | why | step | where |` (Kind/Parent header-declared, either
order, either absent; Kind is `TASK` / `ANSWER` / `DECISION`, where `DECISION` parses to kind `decision`,
wait `answer`); the step cell may end `[Parent: <key>] Options: a = consequence; *b {custom}; c. Deadline: YYYY-MM-DD`
(Parent token before Options; a Parent column wins over the token; `<key>` = `project:ID` or a bare id of an OPEN gate;
unknown/non-gate/closed parent -> md-sync warning, no op; one `depends_on` edit per parent gate, merged with the
gate link, idempotent). md-sync edits choices of an existing OPEN routing item (any source) when a VALID block differs;
absent or malformed block -> live choices untouched (malformed also warns); claimed/in_review items untouched and
reported (`choices-skipped <key>: <state>` line, status file `choices_skipped`).
`tools/qa_backfill.py --questions F --out DIR` proposes choices for rows without a valid block
(`DIR/proposed-rows.md` + `DIR/review.md`, writes nothing else, routing applies what it accepts).

## Knowledge refs (PG-KG-REFS, D-879)

Any item may carry `kg_refs`: <= 20 references `"<graph>:<Type>:<slug>"` to Omnigraph knowledge nodes, Type in
Decision / Rule / Convention / Component / Preference / Project (no Task: plan work lives in the plan graph).
Validated for syntax only, stored sorted and de-duplicated, folded from item events, returned by show/snapshot.
Set by create/edit (L1 own project, router anywhere; MCP `create`/`edit`). The keeper never calls Omnigraph: the
render-time export (PG-KG-EXPORT) resolves refs, follows Supersedes to the current node and reports dangling refs.

## Knowledge export (PG-KG-EXPORT, D-879)

`pg render --out DIR/plan.html` also runs `plangraph.kg_export` (no new service, no listener): it reads Omnigraph
read-only (`POST <base>/graphs/<graph>/query`, bearer token) and writes `DIR/kg-export.json` (atomic). Off by default:

| env | meaning |
|---|---|
| `PG_KG_URL` | Omnigraph base URL (http/https). Unset -> export off |
| `PG_KG_TOKEN_FILE` | file (0600) holding the read-only token. Unset, missing or empty -> export off |
| `PG_KG_GRAPHS` | optional comma list of extra graph ids exported in full (graphs named by `kg_refs` are always read) |
| `PG_KG_STATUS_FILE` | optional status path (default `DIR/kg-export-status.json`) |

Off writes `{"enabled": false}` and exits 0. Any Omnigraph failure exits 0 too (the page renders). A transient failure
(timeout, 5xx, auth, deadline) keeps the last good export; a graph that does not exist (HTTP 404) does not freeze it and
is listed in `unresolved_graphs`. Redirects are never followed and environment proxies are ignored (the token goes only to the configured host).
A query that returns the full 5000-row page sets `truncated` and reports refs missing from that graph as `unresolved_graphs`,
not dangling. The token is never logged or written; timeouts 8 s per call, 60 s overall,
responses capped at 8 MB, the 60 s deadline is checked before every query, <= 12 graphs, <= 1500 listed nodes per graph (`truncated: true` when cut).

`kg-export.json` (enabled): `generated`, `graphs`, `unresolved_graphs` (graph unreadable/absent: refs not judged),
`truncated`, `nodes` {`graph:Type:slug` -> `{graph,type,slug,title,status,severity,kind,excerpt (<=240),project (first hub),
projects (all hubs),superseded_by,current}`}, `items` {item key -> refs}, `reverse` {node ref -> item keys; a superseded ref also lists its
items under the current node}, `projects` {plan project -> `{nodes:[refs]}`: nodes whose Omnigraph Project hub slug equals
the plan project plus nodes its items reference}. `current` follows `Supersedes` to the end of the chain (cycle-safe) to the live node;
`superseded_by` is the direct successor (the accepted one when several Decisions supersede the same node; every link of a chain is exported). Listed per-project knowledge = accepted/proposed Decisions, Rules, Components.
Text is raw data: the page must render it with `textContent`. Credential-shaped strings are masked as in `pg render`.

**Dangling-ref status file** (`kg-export-status.json`, for routerctl; the routerctl flag belongs to routing/autoos):
`{enabled, generated, ok, kept_previous, errors:[{graph,error,missing}], graphs, nodes, unresolved_graphs, dangling:[{ref, items:[item keys]}]}`.
`dangling` = the graph was read and holds no node for the ref (computed from the fresh read even when `kept_previous` is true, so it can name refs the kept export does not contain); refs into unreadable graphs are `unresolved_graphs`, not dangling.
Go-live = a read-only Omnigraph token file from the hypervisor host plus the two env vars on the render host (not part of this lane).

See [Keeper operations](keeper-operations.md) for ready lists, receipts and audits.

Keeper cards retain the computed effort level and reason when written. A
planner may supply an assigned level, and the keeper logs any attempt to
lower it below the computed level.

`pg attempts --since 1d --json` reads terminal spawn rows from keeper storage.
A spawn without a keeper card has a null `card_key`. The display item preserves
the supplied item key and uses `adhoc` when the spawn has no item key. The migration
preserves foreign keys and legacy attempt rows.
Spawn delivery retries through the watchdog, and the keeper deduplicates by
spawn ID. An unwitnessed serving model and unknown usage remain explicit.
