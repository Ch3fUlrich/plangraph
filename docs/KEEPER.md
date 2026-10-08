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
| `store.py` | the verbs (below) as methods of `Store(conn, clock=time.time, verifiers=…, ctx=…, batch_roots=…)`, mixed in from `store_create`/`store_verbs`/`store_review`/`store_waiver`/`store_close`/`store_read`/`store_reap`/`store_support`; every verb takes `(principal, …)` |
| `store_close.py` | `CloseMixin`: the router-only `close_on_decision` verb for tier-`none` items (X7b, "Legacy and decision closes") |
| `evidence.py` | pluggable verifiers (below) |
| `importer.py` | YAML dir → router-attributed batch; `export_yaml(conn, dir)` for round trip |
| `api.py` | HTTP JSON server around `Store` |
| `cli.py` (`python -m plangraph.keeper.cli` / entry `pg`) | thin client of the API; also `admin` subcommands run locally on the keeper host |
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
changes_requested, review_passed, cancelled, priority_set`. `items.state`, `epoch`, `claimed_by`, … are a CACHE written in the same
transaction as the event; `verify_integrity` proves cache == fold.

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
Refusals raise `Refused(code, msg)`; codes: `forbidden, not_found, bad_state, locked, epoch_stale, lease_held, cycle, validation,
evidence_missing, evidence_unverified, same_principal, same_family, family_unknown, conflict`. A refused attempt writes only `audit_log`.
| verb | who | effect |
|---|---|---|
| `create_items(p, items)` | router (any project); l1 (own projects) | batch of definitions as `item_created` events (state `open`); whole batch validated with `views.validate` over existing+new (cycle, unknown dep, unreachable…) or refused atomically |
| `import_items(p, items)` | router only | like `create_items`, but per-item `state` ∈ {open, claimed, in_review} is honoured (legacy rows, e.g. work already in flight): the item gets `item_created` plus a `claimed`/`submitted` event flagged `legacy: true` with `claimed_by` = the item's `owner` string (need not be a principal) and NO verified-evidence requirement; `done` is never importable; the router later attaches evidence (e.g. `decision`) via `add_evidence_router(p, key, kind, ref)` (router-only, state `in_review`/`claimed`, still runs the verifier) |
| `edit_definition(p, key, fields)` | router; l1 own project | only while `open`; fields: title, description, host, estimate_h, done_criteria, evidence_kinds, depends_on, track, owner, note; cross-project `depends_on` changes: router only; whole-graph validate; `item_defined` event |
| `set_priority(p, key, n)` | router | `priority_set` |
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
| `commit+ci` | `{commit, repo?, ci_url?}` | repo = registered path of the project (`repos` config, router-owned); commit exists and is an ancestor of `main`; commit message has trailer `Plan-Item: <key or id>`; commit time ≥ the claim time; CI check-runs for exactly that sha have conclusion `success` (adapter wraps `gh api repos/{o}/{r}/commits/{sha}/check-runs`; `ci: none` allowed only if the project config says it has no CI) |
| `run-record` | `{run_id}` | file `<runs_dir>/<run_id>/job.json` exists, its `request.title` or `task` text contains the item id or key, and `<run_id>/exit.json` has `rc == 0` (format: `job.json` = `{id, run_id, request{card,title,model,…}, task}`, `exit.json` = `{rc, ended}`); runs_dir is config |
| `semaphore-task` | `{task_id, project_id?, template?}` | adapter GET returns status `success` (and template id/name equal to `template` if given); real fetcher reads base URL from env `PG_SEMAPHORE_URL` and the token from the file named by env `PG_SEMAPHORE_TOKEN_FILE`; the token value is never logged or returned; **API shape UNVERIFIED — ship tests with a fake; live check is done later by routing-00/prox** |
| `decision` | `{id: "D-209"}` | line `| D-209 |` exists in the configured DECISIONS.md |
| `commit` (X7b, legacy) | `{commit}` | the sha exists **and** is an ancestor of `main` in the project's registered `repos` path — and *nothing* else: no `Plan-Item` trailer, no claim-time window, no CI. Router-only attachment (see below) |
`evidence_kinds: [any]` = at least one verified evidence of any kind. Unknown kind → `validation`.

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
`edit`, `legacy_state`, `add_evidence`, `close_on_decision`, `cancel`, `priority`) in one
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

**Credential-shape guard (D-374, `credential_guard.py`).** `create_items`/`edit_definition`/`import_items`/`apply_ops` refuse (`validation`, naming the field,
never echoing the value) a `title`/`note`/`done_criteria`/`description` (or any other folded field; `description` is its own column, cap 2000) in which a `word/secretish` token (`\w+/\w{6,}`) sits within 120
characters of `credential|password|passwd|pwd|login`; there are no exemptions, so rephrase paths and URLs near credential words.
`pg render` masks any match that is already stored as `[redacted]` and prints `render: masked N credential-shaped string(s)` on stderr.
