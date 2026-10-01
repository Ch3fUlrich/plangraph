# Keeper contract (M2a) — store, state machine, evidence, API, CLI, MCP

Builds on docs/SPEC.md (item shape, views) and docs/PROPOSAL.md §3. Python 3.12 **stdlib only** (sqlite3, http.server, hashlib,
secrets, json, graphlib, subprocess, urllib); PyYAML only in the importer. Package `src/plangraph/keeper/`. Every file ≤ 400 lines,
tests first (pytest), no network in tests (fakes), no secrets in code/logs/tests. M2a = per-principal tokens; M2b (later) adds
run-bound tokens, signed snapshot, outbox, anchor — leave clean seams, do not build them.

## Modules
| file | content |
|---|---|
| `schema.py` | DDL + triggers as SQL strings; `SCHEMA_VERSION` |
| `db.py` | `open_db(path, *, create=False) -> sqlite3.Connection`: the ONLY way the keeper opens the DB. Sets `foreign_keys=ON`, `recursive_triggers=ON`, WAL, `busy_timeout`, `trusted_schema=OFF`, and `set_authorizer` denying DDL (`CREATE/DROP/ALTER`, `DROP TRIGGER`), `PRAGMA writable_schema`, `ATTACH`, `load_extension` after the schema is installed. Schema install uses a separate `create=True` path. Singleton `flock` helper `lock_keeper(path)`. |
| `events.py` | event append with hash chain (`hash = sha256(prev_hash + canonical_json(event))`), `fold(events) -> state`, `verify_chain(conn) -> list[problem]`, `verify_integrity(conn)` (cache `items.state/epoch/claimed_by` equals fold) |
| `auth.py` | principals + tokens: `add_principal(conn, name, role, projects, family=None) -> token` (shown once; only sha256 stored), `authenticate(conn, token) -> Principal`, `revoke`. Roles: `router`, `l1`, `worker`, `reviewer`, `system`, `reader`. |
| `store.py` | the verbs (below) as methods of `Store(conn, clock=time.time, verifiers=…)`; every verb takes `(principal, …)` |
| `evidence.py` | pluggable verifiers (below) |
| `importer.py` | YAML dir → router-attributed batch; `export_yaml(conn, dir)` for round trip |
| `api.py` | HTTP JSON server around `Store` |
| `cli.py` (`python -m plangraph.keeper.cli` / entry `pg`) | thin client of the API; also `admin` subcommands run locally on the keeper host |
| `mcp.py` | stdio MCP server (hand-written JSON-RPC 2.0: `initialize`, `tools/list`, `tools/call`; no SDK), same verbs, calls the API |

## Tables (all in one SQLite file)
`principals(name PK, role, projects json, family, token_sha256, active)` ·
`items(key PK, project, id, title, kind, track, owner, estimate_h, estimate_src, done_criteria, evidence_kinds json, operator,
priority, source, note, corrects, state, epoch, claimed_by, delegate, lease_until, reviewer, model, created_at, updated_at)` ·
`edges(src, dst, rel CHECK rel IN ('depends_on'))` (`corrects` lives on the item; `src` depends on `dst`) ·
`events(seq INTEGER PK AUTOINCREMENT, ts, actor, role, model, key, type, payload json, prev_hash, hash)` append-only ·
`evidence(id PK, key, epoch, kind, ref json, verified, detail, at)` ·
`audit_log(id PK, ts, actor, role, action, key, ok, code, msg)` — **every** attempt incl. refused ones; never locked ·
`audit_findings(id PK, ts, key, code, msg)`.
Event types: `item_created, item_defined, claimed, delegated, released, lease_expired, evidence_added, submitted,
changes_requested, review_passed, cancelled, priority_set`. `items.state`, `epoch`, `claimed_by`, … are a CACHE written in the same
transaction as the event; `verify_integrity` proves cache == fold.

## Locking (prevention)
Triggers key off `EXISTS (SELECT 1 FROM events WHERE key=… AND type='review_passed')` (never the cache), and also treat
`cancelled` as locked: `BEFORE UPDATE/DELETE` on `items` (any column except none — DONE rows are fully frozen), on `edges` where
`src` is locked, on `evidence`/`events` of a locked key; `BEFORE INSERT` on `events`/`evidence`/`edges` for a locked key, and on
`items` when the key already exists in a locked state (fires for `INSERT OR REPLACE`). New items MAY depend on locked/DONE items
and may set `corrects` to a DONE key (validated by the store: target must be `done`). Events table: `BEFORE UPDATE/DELETE` always abort.
`principals`/`audit_log` are not locked.

## Verbs (Store) — role rules, state machine, errors
Every verb: authenticate → authorise → validate → one transaction (event + cache + edges) → audit_log row (ok or refused).
Refusals raise `Refused(code, msg)`; codes: `forbidden, not_found, bad_state, locked, epoch_stale, lease_held, cycle, validation,
evidence_missing, evidence_unverified, same_principal, same_family, family_unknown, conflict`. A refused attempt writes only `audit_log`.
| verb | who | effect |
|---|---|---|
| `create_items(p, items)` | router (any project); l1 (own projects) | batch of definitions as `item_created` events (state `open`); whole batch validated with `views.validate` over existing+new (cycle, unknown dep, unreachable…) or refused atomically |
| `import_items(p, items)` | router only | like `create_items`, but per-item `state` ∈ {open, claimed, in_review} is honoured (legacy rows, e.g. work already in flight): the item gets `item_created` plus a `claimed`/`submitted` event flagged `legacy: true` with `claimed_by` = the item's `owner` string (need not be a principal) and NO verified-evidence requirement; `done` is never importable; the router later attaches evidence (e.g. `decision`) via `add_evidence_router(p, key, kind, ref)` (router-only, state `in_review`/`claimed`, still runs the verifier) |
| `edit_definition(p, key, fields)` | router; l1 own project | only while `open`; fields: title, estimate_h, done_criteria, evidence_kinds, depends_on, track, owner, note; cross-project `depends_on` changes: router only; whole-graph validate; `item_defined` event |
| `set_priority(p, key, n)` | router | `priority_set` |
| `claim(p, key, lease_s=7200)` | l1 (own project), worker | `open`+ready (all deps `done`) → `claimed`, epoch+1, `lease_until`; atomic (BEGIN IMMEDIATE); second claimant gets `conflict` |
| `delegate(p, key, worker_principal)` | claimant (l1) | sets `delegate` (the worker principal; it must have role `worker`) |
| `heartbeat(p, key, epoch)` | claimant or delegate | extends lease; stale epoch → `epoch_stale` |
| `release(p, key, epoch)` | claimant, router | `claimed`→`open` |
| `add_evidence(p, key, epoch, kind, ref)` | claimant or delegate, state `claimed` | runs the verifier for `kind`; stores `verified` + detail; unverified evidence is stored but does not count |
| `submit(p, key, epoch)` | claimant or delegate | `claimed`→`in_review` iff every kind in `evidence_kinds` has verified evidence; lease frozen (cleared) |
| `request_changes` / `pass_review` as `verdict(p, key, ok, reason)` | reviewer | `in_review`: ok → records `review_verdict`; when two distinct reviewers (neither claimant/delegate, two distinct resolved families unless waived) have passed, re-verifies ALL evidence → `done` (event `review_passed`, locks); not ok → `claimed` (same epoch; 2nd time in a row → `open` + audit_finding). Refused: reviewer == claimant/delegate (`same_principal`); reviewer.family == delegate-or-claimant family (`same_family`); either family NULL (`family_unknown`, fail closed) |
| `waive_second_family(p, key, D-NNN)` | router | `claimed`/`in_review`: runs the `decision` verifier on `{id: D-NNN}`; on success emits `waiver_set` and sets `items.review_waiver`, so the two passing verdicts may share a family (D-217). Refused: non-router (`forbidden`), unverified decision (`evidence_unverified`), DONE (`locked`), other states (`bad_state`) |
| `cancel(p, key, reason)` | router; l1 own project | `open|claimed|in_review`→`cancelled`; refused from `done` (`locked`) |
| `reap(now)` | system | `claimed` with `lease_until < now` → `open` (`lease_expired`); `in_review` stuck > 4 h → audit_finding `review_overdue` (no state change) |
**D-219 review rule (operator; supersedes the single-verdict `done` above):** one implementer + exactly TWO reviewers per change, no extra Sonnet final. `verdict(ok)` records a `review_verdict` event per reviewer (current epoch/submission round); `done` (`review_passed`) is emitted only when exactly two distinct reviewer principals — neither the claimant nor the delegate, and from two distinct RESOLVED families — have both passed and the evidence re-verifies; a third verdict is refused (`conflict`), a `--changes` verdict from either seat returns the item to `claimed` and voids the round's verdicts. Waiver path (D-217, e.g. the second family's seat is down > 1 h): router-only `waive_second_family(key, decision_id)` — requires a verified `decision` record D-id — lets the two verdicts share a family; the waiver is recorded in the event, shown by `show`, `digest` and the snapshot (`review_waiver: D-217`). Per-item `reviews_required` is fixed at 2 (not editable).
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
| `commit+ci` | `{commit, repo?, ci_url?}` | repo = registered path of the project (`repos` config, router-owned); commit exists and is an ancestor of `main`; commit message has trailer `Plan-Item: <key or id>`; commit time ≥ the claim time; CI check-runs for exactly that sha have conclusion `success` (adapter wraps `gh api repos/{o}/{r}/commits/{sha}/check-runs`; `ci: none` allowed only if the project config says it has no CI) |
| `run-record` | `{run_id}` | file `<runs_dir>/<run_id>/job.json` exists, its `request.title` or `task` text contains the item id or key, and `<run_id>/exit.json` has `rc == 0` (format: `job.json` = `{id, run_id, request{card,title,model,…}, task}`, `exit.json` = `{rc, ended}`); runs_dir is config |
| `semaphore-task` | `{task_id, project_id?, template?}` | adapter GET returns status `success` (and template id/name equal to `template` if given); real fetcher reads base URL from env `PG_SEMAPHORE_URL` and the token from the file named by env `PG_SEMAPHORE_TOKEN_FILE`; the token value is never logged or returned; **API shape UNVERIFIED — ship tests with a fake; live check is done later by routing-00/prox** |
| `decision` | `{id: "D-209"}` | line `| D-209 |` exists in the configured DECISIONS.md |
`evidence_kinds: [any]` = at least one verified evidence of any kind. Unknown kind → `validation`.

## API (stdlib `http.server`, threaded; one Store with a lock; bind 127.0.0.1 by default, `--host`)
`POST /v1/<verb>` JSON body → JSON; `GET /v1/{ready,show,blocked,waves,critical_path,percent,alerts,digest,snapshot,whoami,health}`;
`Authorization: Bearer <token>` (health is open). Errors: `{"error": code, "msg": …}` with 4xx. Compact text responses where the verb is
meant for agents (`ready`, `show`, `alerts`) via `?fmt=text` (default text for CLI). The server also runs the reaper every 60 s and rewrites
`plan.snapshot.json` (path from config) after every committed write. Config file (JSON, mode 600 checked): db path, snapshot path, repos
{project: path}, runs_dir, decisions_file, ci per project, bind, lease defaults. No config value that is a secret is ever printed.

## CLI `pg`
`pg ready [--project P] [--all] [-n 10]`, `show KEY`, `claim KEY [--lease 2h]`, `delegate KEY WORKER`, `heartbeat KEY`, `release KEY`,
`evidence KEY --kind K --commit SHA | --run ID | --task N | --decision D-NNN`, `submit KEY`, `verdict KEY --pass|--changes "why"`,
`waive KEY D-NNN`, `cancel KEY`, `priority KEY N`, `define …`, `blocked`, `waves`, `critical`, `percent`, `alerts`, `digest`, `snapshot`, `whoami`,
`render --out F [--cdn]`; admin (local, on the keeper host): `pg admin init|add-principal|revoke|verify|import DIR|export DIR`.
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
