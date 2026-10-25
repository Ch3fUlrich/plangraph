# Keeper contract (M2a) — store, state machine, evidence, API, CLI, MCP

Builds on docs/SPEC.md (item shape, views) and docs/PROPOSAL.md §3. Python 3.12 **stdlib only** (sqlite3, http.server, hashlib,
secrets, json, graphlib, subprocess, urllib); PyYAML only in the importer. Package `src/plangraph/keeper/`. Every file ≤ 400 lines,
tests first (pytest), no network in tests (fakes), no secrets in code/logs/tests. M2a = per-principal tokens; M2b (later) adds
run-bound tokens, signed snapshot, outbox, anchor — leave clean seams, do not build them.

## Modules
| file | content |
|---|---|
| `schema.py` | DDL + triggers as SQL strings; `SCHEMA_VERSION` = 13 (main's pinned code-level version), `OVERSIGHT_SCHEMA_VERSION` = 14 (v14 = the oversight `OVERSIGHT_DDL` appended after main's `JEV_DDL`, installed by `db._upgrade`: tables/indexes presence-based, the four oversight **guard triggers dropped and re-created on every open** so a weakened body cannot survive — SQLite's open path; Postgres installs the same guards once in Migration 8, GAP: no per-open repair there, D-2458); main's #140 exec ownership columns (`items.exec_workflow_id/exec_run_id/exec_attempt/exec_principal/exec_retired_runs`, also `db._upgrade`) stay in the `items` DDL at main's v13 shape, PG Migration 7 |
| `db.py` | `open_db(path, *, create=False) -> sqlite3.Connection`: the ONLY way the keeper opens the DB. Sets `foreign_keys=ON`, `recursive_triggers=ON`, WAL, `busy_timeout`, `trusted_schema=OFF`, and `set_authorizer` denying DDL (`CREATE/DROP/ALTER`, `DROP TRIGGER`), `PRAGMA writable_schema`, `ATTACH`, `load_extension` after the schema is installed. Schema install uses a separate `create=True` path. Singleton `flock` helper `lock_keeper(path)`.  Optional Postgres backend (`backend: postgres`, `dsn`/`PG_DSN`): see `docs/keeper-postgres.md`. |
| `events.py` | event append with hash chain (`hash = sha256(prev_hash + canonical_json(event))`), `fold(events, unknown=None) -> state`, `verify_chain(conn) -> list[problem]`, `verify_integrity(conn)` (cache `items.state/epoch/claimed_by` equals fold + one `unknown_event_type` problem per event `type` outside `EVENT_TYPES`) |
| `auth.py` | principals + tokens: `add_principal(conn, name, role, projects, family=None) -> token` (shown once; only sha256 stored), `authenticate(conn, token, *, now=None) -> Principal`, `revoke`. Roles: `router`, `l1`, `worker`, `reviewer`, `system`, `reader`, `spawner`, `oversight`. `add_oversight_principal(conn, name, projects)` provisions the revocable row (no token); a `pgo1_` token authenticates as `OversightPrincipal` **and is refused there once `expires_at` has passed** (the TTL is checked at authentication, judged by `now` or by wall time (`time.time()`); dispatch resolves the principals table first, so a prefixless token whose bytes start `pgo1_` still resolves as itself, and `add_principal` re-rolls any draw that would). `add_principal` refuses `role="oversight"`. |
| `store_oversight.py` | `OversightMixin`: local-only `mint_oversight_token(name, go_decision=…, reason=…)` (TTL 3600 s, generation rotation, sha256 only; `prepare_oversight_token`/`commit_oversight_token` re-validate the live env, the GO decision row, the TTL bound and the prepare's own digest at commit), the per-use token re-check, the derived hourly budgets (**consulted by every writer before it inserts**), and the `oversight_ops` audit rows + one open `oversight-guard` alarm per principal. |
| `oversight_policy.py` | pure policy: `check_action(verb)` and `check_batch(items_by_key, ops, projects) -> [Verdict]` (the single grant, project-scoped, used by both fail-closed seams; malformed op data answers with a fail-closed `malformed` verdict, never an exception). |
| `store.py` | the verbs (below) as methods of `Store(conn, clock=time.time, verifiers=…, ctx=…, batch_roots=…)`, mixed in from `store_create`/`store_verbs`/`store_review`/`store_waiver`/`store_close`/`store_read`/`store_reap`/`store_support`; every verb takes `(principal, …)` |
| `store_close.py` | `CloseMixin`: the router-only `close_on_decision` verb for tier-`none` items (X7b, "Legacy and decision closes") |
| `evidence.py` | pluggable verifiers (below) |
| `importer.py` | YAML dir → router-attributed batch; `export_yaml(conn, dir)` for round trip |
| `api.py` | HTTP JSON server around `Store` |
| `cli.py` (`python3 -m plangraph.keeper.cli` / entry `pg`) | thin client of the API; also `admin` subcommands run locally on the keeper host |
| `mcp.py` | stdio MCP server (hand-written JSON-RPC 2.0: `initialize`, `tools/list`, `tools/call`; no SDK), same verbs, calls the API |

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
changes_requested, review_passed, cancelled, priority_set, kind_set`. `items.state`, `epoch`, `claimed_by`, … are a CACHE written in the same
transaction as the event; `verify_integrity` proves cache == fold.

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
evidence_missing, evidence_unverified, same_principal, same_family, family_unknown, conflict, exec_stale, exec_stale_state`. A refused attempt writes only `audit_log`.
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
| `claim(p, key, lease_s=7200)` | l1 (own project), worker | `open`+ready (all deps `done`) → `claimed`, epoch+1, `lease_until`; atomic (BEGIN IMMEDIATE); second claimant gets `conflict` |
| `delegate(p, key, worker_principal)` | claimant (l1) | sets `delegate` (the worker principal; it must have role `worker`) |
| `heartbeat(p, key, epoch)` | claimant or delegate | extends lease; stale epoch → `epoch_stale` |
| `release(p, key, epoch)` | claimant, router | `claimed`→`open` |
| `add_evidence(p, key, epoch, kind, ref)` | claimant or delegate, state `claimed` | runs the verifier for `kind`; stores `verified` + detail; unverified evidence is stored but does not count |
| `submit(p, key, epoch)` | claimant or delegate | `claimed`→`in_review` iff every kind in `evidence_kinds` has verified evidence; lease frozen (cleared) |
| `request_changes` / `pass_review` as `verdict(p, key, ok, reason)` | reviewer | `in_review`: ok → records `review_verdict` and re-verifies ALL evidence; on a `standard` item this one cross-family verdict → `done` (event `review_passed`, locks), on a `high` item a second distinct reviewer (two distinct resolved families unless waived) is required; not ok → `claimed` (same epoch; 2nd time in a row → `open` + audit_finding). Refused: reviewer == claimant/delegate (`same_principal`); reviewer.family == delegate-or-claimant family (`same_family`); either family NULL (`family_unknown`, fail closed) |
| `waive_second_family(p, key, D-NNN)` | router | **`high` items only (D-228)** — `claimed`/`in_review`: runs the `decision` verifier on `{id: D-NNN}` **and** requires that D-row to contain the whole token `Waives: <key>` (X6c3); on success emits `waiver_set` and sets `items.review_waiver`, so the two passing verdicts may share a family (D-217). Refused: non-router (`forbidden`), unverified decision or a D-row that does not name the key (`evidence_unverified`), a `standard` item (`bad_state`: one verdict already suffices, there is no second family seat), DONE (`locked`), other states (`bad_state`) |
| `apply_ops(p, ops, batch_ref=None)` | router only | B1: BATCH.md ops in one transaction, all or nothing (`store_ops.py`; paragraph below) |
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

Both tiers record one `review_verdict` event per passing verdict (current epoch/submission round) and re-verify ALL evidence with the same logic inside the same write lock, so a stale-evidence refusal never depends on which seat spoke first. `standard`: **one** passing verdict is enough — the reviewer must be a distinct principal (not the claimant, not the delegate) whose normalised non-blank family differs from the writer's (`_reviewer_family_ok`, fail closed on a blank family on either side) — and the item goes `done` + `review_passed` immediately. `high`: unchanged D-219 — one implementer plus exactly TWO reviewers, no extra Sonnet final; `done` only when two distinct reviewers from two distinct RESOLVED families have both passed, a third verdict is refused (`conflict`). Waiver path (D-217, e.g. the second family's seat is down > 1 h): router-only `waive_second_family(key, decision_id)` — requires a verified `decision` record D-id **whose DECISIONS.md row contains `Waives: <project:ID>` for exactly that key** (X6c3: whole-token match, so `Waives: demo:A` does not waive `demo:AB`; one D-row may list several `Waives:` tokens; tokens inside fenced/indented code blocks do not count) — lets the two verdicts share a family; the waiver is recorded in the event, shown by `show`, `digest` and the snapshot (`review_waiver: D-217`). Waiving a `standard` item is refused (`bad_state`). A `verdict(ok=False)` from either seat of either tier returns the item to `claimed` and voids the round's verdicts. Per-item `reviews_required` is fixed at 2 for `high` and 1 for `standard` (not editable).
Allowed edges only: open→claimed, claimed→open, claimed→in_review, in_review→claimed, in_review→done, open|claimed|in_review→cancelled.
Reads (any authenticated principal): `ready(project=None, n=10)` (text `views.brief`), `show(key)` (compact: fields, deps with states, unblocks,
evidence, lease; ≤ ~300 tokens), `blocked(project)`, `waves()`, `critical_path(project)`, `percent(project)`, `alerts(project)` (lease expiring < 15 min,
overdue reviews, audit_findings, stall = no ready + nothing in flight + goal unreached, refusals ≥2 on a key), `digest()` (router: per-project counts, stalled, last
10 DONE with evidence kinds), `snapshot()` (SPEC snapshot JSON; `head` = last event hash), `whoami()`.

## Evidence verifiers (`evidence.py`)
`Result(ok: bool, detail: str, ref: dict)`; registry `VERIFIERS: dict[str, Verifier]`; `Verifier.verify(item, ref, ctx) -> Result`; ctx holds
injected adapters so tests use fakes: `ctx.git`, `ctx.ci`, `ctx.runs`, `ctx.semaphore`, `ctx.decisions`, `ctx.now`.
| kind | ref | passes when |
|---|---|---|
| `commit+ci` | `{commit, repo?, ci_url?}` | repo = registered path of the project (`repos` config, router-owned); commit exists and is an ancestor of `main`; commit message has trailer `Plan-Item: <key or id>`; commit time ≥ the claim time; CI check-runs for exactly that sha have conclusion `success` (adapter wraps `gh api repos/{o}/{r}/commits/{sha}/check-runs`; a `forgejo:<owner>/<repo>` value reads Forgejo commit statuses instead — see "Forgejo CI statuses" below; `ci: none` allowed only if the project config says it has no CI) |
| `run-record` | `{run_id}` | file `<runs_dir>/<run_id>/job.json` exists, its `request.title` or `task` text contains the item id or key, and `<run_id>/exit.json` has `rc == 0` (format: `job.json` = `{id, run_id, request{card,title,model,…}, task}`, `exit.json` = `{rc, ended}`); runs_dir is config |
| `semaphore-task` | `{task_id, project_id, template}` (all positive integers; `template` = template id, REQUIRED, a name is refused) | adapter `GET {base}/api/project/{project_id}/tasks/{task_id}` returns status `success`, `id`/`project_id` matching the ref, and `template_id` equal to `template` (a response without `template_id` fails); the project must be listed in env `PG_SEMAPHORE_PROJECTS` (comma-separated positive ints; unset/empty/malformed fails closed with "semaphore project allowlist not configured", otherwise "project not allowed", before any request); HTTP 401/403 report "semaphore auth refused (code)", other HTTP errors "semaphore request failed (HTTP code)"; real fetcher reads base URL from env `PG_SEMAPHORE_URL` and the token from the file named by env `PG_SEMAPHORE_TOKEN_FILE`; the token value is never logged or returned; endpoint `/api/project/{project_id}/tasks/{id}` (Semaphore UI API) |
| `decision` | `{id: "D-209"}` | line `| D-209 |` exists in the configured DECISIONS.md |
| `commit` (X7b, legacy) | `{commit}` | the sha exists **and** is an ancestor of `main` in the project's registered `repos` path — and *nothing* else: no `Plan-Item` trailer, no claim-time window, no CI. Router-only attachment (see below) |
`evidence_kinds: [any]` = at least one verified evidence of any kind. Unknown kind → `validation`.

### Forgejo CI statuses (PG-CI-FORGEJO)

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
failure. Proposed, not applied: `docs/proposals/keeper-ci-config.md`.

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

**Evidence kind `commit`** is the historical twin of `commit+ci`: it passes on "this sha
exists and is an ancestor of `main` in the project's registered repo" and deliberately drops
the trailer, claim-window and CI checks a migrated item can no longer satisfy.  That makes
it a weaker proof, so it is attachable **only by the router through
`add_evidence_router`**; a claimant or delegate naming kind `commit` is refused `forbidden`
and stores nothing.  Unknown project (no `repos` entry), missing repo and any git error all
fail closed as unverified.

## API (stdlib `http.server`, threaded; one Store with a lock; bind 127.0.0.1 by default, `--host`)
`POST /v1/<verb>` JSON body → JSON; `GET /v1/{ready,show,blocked,waves,critical_path,percent,alerts,digest,snapshot,whoami,health}` plus
`GET /v1/audit-meta?project=&stale_days=` (router/L1 only, and `GET /v1/changelog?since=`);
`Authorization: Bearer <token>` (health is open). Errors: `{"error": code, "msg": …}` with 4xx. Compact text responses where the verb is
meant for agents (`ready`, `show`, `alerts`) via `?fmt=text` (default text for CLI). The server also runs the reaper every 60 s and rewrites
`plan.snapshot.json` (path from config) after every committed write. Config file (JSON, mode 600 checked): db path, snapshot path, repos
{project: path}, runs_dir, decisions_file, ci per project (plus `forgejo_url` + `forgejo_token_file`
for `forgejo:` values), bind, lease defaults. No config value that is a secret is ever printed.

## CLI `pg`
`pg ready [--project P] [--all] [-n 10]`, `show KEY`, `claim KEY [--lease 2h]`, `delegate KEY WORKER`, `heartbeat KEY`, `release KEY`,
`evidence KEY --kind K --commit SHA | --run ID | --task N | --decision D-NNN`, `submit KEY`, `verdict KEY --pass|--changes "why"`,
`waive KEY D-NNN`, `cancel KEY`, `priority KEY N`, `define …`, `blocked`, `waves`, `critical`, `percent`, `alerts`, `digest`, `snapshot`, `whoami`,
`render --out F [--cdn]`, `audit-meta [--project P] [--stale-days N] [--skeleton DIR]`; admin (local, on the keeper host): `pg admin init|add-principal|revoke|verify|import DIR|export DIR`.
Env `PG_URL`, `PG_TOKEN_FILE` (path of a mode-600 file; never pass a token on argv). Default output = the smallest useful text; `--json` for full.
Exit codes: 0 ok, 2 refused (stderr `code: msg`), 3 unreachable (CLI then reads the local `plan.snapshot.json` read-only for read verbs and says so).

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

Waiver rule (D-217 / X6c3): `waive_second_family(key, D-NNN)` succeeds only if the D-NNN row in the decisions file contains `Waives: <project:ID>`
for exactly that key (whole token; a row may name several keys; code blocks do not count). `Waives:` and `Closes:` tokens count only inside the DECISION cell (3rd column) of their D-row (D-276). A forged D-id still needs the decision verifier, so the
residual risk above applies to waivers too: a raw-SQL writer who can edit DECISIONS.md can name any key.

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
`depends_on`: they read the item's current edges inside the transaction (later ops see earlier ones) and delegate to the very same `edit_definition` write, so auth, `open`-state, cycle/unknown-dep/`_MAX_DEPS` validation and the `item_defined` event are the edit's. A dep `add_dep` already has or `remove_dep` lacks writes nothing, appends no event and answers `{"key", "warnings": [...]}`; a dep naming an item whose state is `cancelled` is dropped from the resulting list with `warnings ["dropped cancelled dependency <key>"]` (a full `edit` of `depends_on` drops it the same way, so `views.validate`'s `depends_on_cancelled` no longer refuses the write), an unknown dep key is still `validation`. The result keeps `{"key": ...}` alone unless something was dropped or skipped.

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
Writes still go to the keeper through the host's existing channel; a run token's writes queue in the
outbox while the keeper is unreachable (M2B-OUTBOX).

- Keeper side: `python3 -m plangraph.keeper.snapshot_serve --config keeper.json` is the forced
  command of a `restrict` authorized_keys line (one pull key per host). It prints one JSON line
  `{"format": "plangraph-snapshot-pull-v1", "snapshot": <base64>, "sig": <base64>}` and nothing
  else, ignores `SSH_ORIGINAL_COMMAND` and stdin, opens files with `O_NOFOLLOW`, and serves nothing
  unless signing is configured and the snapshot verifies.
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
listed in the md-sync status file under `unmatched`.
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

## Router retier of an in-review item (D-950 rule)

The router may `edit` `review_tier` / `evidence_kinds` of a `claimed` or `in_review` item (the keeper allows it; an L1
edit of a non-open item is refused). Fleet rule (routing, 2026-10-10): use it ONLY for ops/config items that produced no
code commit, and only with a D-row naming the key in `Closes:`; md-sync then closes the item on that decision. It is
audited: the hash-chained `item_defined` event records `"while": "<state>"` and the new tier
(the keeper `events` table; the activity page shows recent events), so a retier during review is visible. `withdraw_review` stays the path for items
that are decision-closable by their own definition.

## Next item for a heartbeat (PG-READY-OWNER, D-953)

`pg ready --project P --mine --agent -n 1` (HTTP `GET /v1/ready?project=P&mine=1&agent=1&n=1`, MCP `ready` with
`mine`/`agent`) returns the best item the caller can start now: state `open`, every dependency `done`, owned by the caller
(`--owner NAME` for another principal), operator steps excluded (`operator` flag or kind question/decision/
operator_task), ordered by (-priority, -unblocks, estimate, key). Then `pg claim KEY --lease 2h`. Offline (keeper
unreachable) the CLI answers from the snapshot; `--mine` then cannot resolve the caller and lists all owners.

## Change log (PG-CHANGELOG-GEN)

`changelog(since)` (read verb, any authenticated principal; `GET /v1/changelog?since=...` text by default, `fmt=json` for
`{since, head_seq, entries}`; `pg changelog --since DATE|SEQ`) folds the hash-chained events into one line per change:
`created` (`item_created`), `closed` (`review_passed` = review, `closed_on_decision` = decision ids), `answered`
(`answered` + decision id), `cancelled` (reason) and `restart` (`outage_recorded`: the keeper came back after a gap; a restart
without a gap leaves no event). Decision ids carry the DECISION cell of their row in the configured decisions file (cut to
100 chars; ids stay bare when the file is unreadable). `--since` is an event seq (entries with a greater seq) or a date /
ISO datetime (entries at or after it, UTC). The text is a pure function of the events: header
`changelog since X | head #N | M entries`, `## YYYY-MM-DD` groups, `HH:MMZ #seq type key | title | detail` lines, a
`totals:` line (`no changes` when empty). Freezing the result into CHANGELOG.md is a manual step.

## Plan metadata audit (PG-AUDIT-META, D-1012/D-1013)

`audit_meta(principal, project=None, stale_days=3)` (`store_audit_meta.py`; the pure logic, the text render and the
skeleton writer are `audit_meta.py`) answers two questions from the keeper's own rows: which items lack the metadata a
worker needs before it can start, and which claims have gone quiet. Nothing is fetched: no git, no network, and the age
of a claim is measured against the **injected clock**, never the wall clock. Doors: CLI
`pg audit-meta [--project P] [--stale-days N] [--skeleton DIR]`, HTTP `GET /v1/audit-meta?project=&stale_days=`
(text by default, `fmt=json` for the data below), MCP read tool `audit_meta` (`project`, `stale_days`).
Roles: **router and l1 only** — any other role is `forbidden` (403), as is a run token (`run_gate`). An l1 sees only its
own projects; `--project` naming another one is refused with the same `forbidden` every other cross-project request uses,
and the message names only the project — never its keys or counts. `stale_days` is validated (whole number, 1..9999)
**before** anything is read, so a bad value costs no query. A successful read is exit 0 and one `audit_log` row like every
verb, refusal included.

### Missing metadata

Only `open` and `claimed` items are audited: an `in_review` item is with its reviewer, and `done`/`cancelled` items are
locked and cannot be edited. A text field is missing when it is NULL or whitespace-only; `estimate_h` is numeric, so it is missing when NULL or when `estimate_src` is `default`.

| field | kinds not audited | also missing when |
|---|---|---|
| `description` | none | — |
| `estimate_h` | `gate`, `question`, `decision`, `operator_task` | `estimate_src` is the `default` marker (SPEC: the plan stated no number, the stored figure is a placeholder) |
| `done_criteria` | `gate`, `question`, `decision`, `operator_task` | — |
| `owner` | none | — |

A gate is a checkpoint and an operator kind waits on an answer or a task, so neither is estimated work; every kind is
still audited for `description` and `owner`, which every item needs. Per project each field reports `{field, count,
keys}` with the keys sorted; `missing_items` counts an item once however many fields it lacks; `stubs` carries one
skeleton entry per such item (`key`, `title`, `state`, `fields`, `current` — the four audited values as cells).
`totals` repeats `{projects, audited, fields, missing_items, stale_items}` across the caller's scope.

### Stale claims

A `claimed` item whose **newest event** is older than `stale_days` days. A `heartbeat` extends the lease without
appending an event, so a claim that only keeps its lease alive still ages out here: that is what `renew` means below.
Groups are keyed by `claimed_by`, falling back to `owner` when it is empty (a legacy import claims by an owner string)
and to `(none)` when both are, sorted by holder then key; each entry is `{key, claimed_by, last_ts, age_days, action}`.
The suggested action is the first match:

| action | when | what the router does |
|---|---|---|
| `close-by-D-row` | the key carries verified `commit` or `commit+ci` evidence — exactly the snapshot's `merged` rule, read from the `evidence` table, so the audit stays cheap and read-only (no git call; an unverified or non-commit row never triggers it) | the work is on `main`: write the decision that names the key in `Closes:` and close it (or review it, if its tier is not `none`) |
| `release` | no claimant name, or a name that is not a stored principal, or the last event is older than **2×N** days | the claim is held by nobody the keeper can reach, or has been quiet twice over: `pg release` it back to the pool |
| `renew` | otherwise (known claimant, between N and 2N days quiet) | probably still live work: ask for a heartbeat, or release it if the answer does not come |

The text is a pure function of the rows and the injected `now`: header `audit-meta now <iso> | stale-days N | projects
a, b`, one `P: missing X items \| stale Y claims` block per project with a `field  count  keys` line for each audited
field (`-` when none), a `  stale HOLDER (n)` group and `    KEY  last TS  age Nd  -> action` lines, then a `totals:`
line. `--json` carries the same data, byte-for-byte equivalent.

### `pg audit-meta --skeleton DIR`

The verb only returns data; the CLI writes the files. One `<project>.BATCH.yaml` per project **that has findings**
(silent projects write nothing), plus its sibling note, and the directory is created if missing; the written paths go to
stderr so stdout stays the text (or `--json`, which still writes the files). The YAML is `docs/BATCH.md` shape
(`batch: audit-meta-<project>`, `principal: router`, `source: <project>.BATCH.md`, `source_sha256`, `ops:`), and
`source_sha256` is computed over the note's bytes as they are written, so `python3 tools/apply_batch.py DIR/P.BATCH.yaml
--dry-run` (validation and digest check, no HTTP) accepts it — which the tests prove by importing `tools/apply_batch.py`.
Project names are made filesystem-safe (`[^A-Za-z0-9._-]` → `_`).

Exactly one `op: edit` per item, listing only the missing fields, each value an empty string: `apply_batch`'s validator
accepts `""` (checked), so the stub parses as-is and the emptiness is the point — nothing here proposes a value. Two YAML
comments precede each op: the item's `key | title | state` + the missing fields, and its current `description=… |
estimate_h=… | done_criteria=… | owner=…`. The note file explains all of that and two caveats: `principal: router` is a
placeholder (an applier needs a router token of that name; an l1 has no batch door and edits with `pg define --edit`),
and an item listed as `state claimed` cannot be edited until it is released, because `edit_definition` only accepts
`open` items.


## Backup retention (PG-BACKUP-RETENTION)

The keeper writes no backups itself; operators leave ad-hoc copies (`backup-pre-*.db`, `keeper.db.pre-*`, `keeper.db.bak-*`, `pre-*.db`, `keeper-pre-*.db`) in the keeper data dir, and they pile up. `pg admin prune-backups` trims them. It needs no keeper config and has no default directory.

    pg admin prune-backups --dir DIR [--keep 10] [--daily-days 7] [--apply]

- Policy: keep the newest `--keep` files by mtime, plus the newest file of each of the last `--daily-days` UTC calendar days; delete the rest. Files with a future mtime are kept.
- **Dry run is the default**: it lists what would go, bytes freed and `du before` / `du after (projected)`, and deletes nothing. Only `--apply` unlinks. Exit 0 ok, 2 bad arguments or directory (`/`, empty, missing), 1 if any delete failed.
- Never touched, whatever the name: `keeper.db`, `keeper.json` (and `keeper.json.*`), `*.token`, `*-wal`, `*-shm`, `*.lock`, `*.pid`, `*.alive`, `*.log`, `plan.snapshot*`, `md-sync*`, symlinks, sub-directories (not entered; no recursion) and any name outside the explicit allow-list in `backup_retention.py`.
- Opt-in keeper hook (default off), in `keeper.json`: `"backup_prune": {"enabled": false, "dir": "<path>", "keep": 10, "daily_days": 7, "interval_s": 21600}`. When `enabled`, the daemon prunes (apply) once after startup and then every `interval_s` on a daemon thread that stops on shutdown; errors go to stderr and never stop the keeper. `enabled` without `dir` is a config error.
- Alternative to the hook: run `pg admin prune-backups --dir DIR --apply` from a Semaphore playbook or cron job.
