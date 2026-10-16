# Keeper contract 1.0

Version: **1.0** (frozen). Companion files:

* `keeper-contract-v1-routes.md` - every HTTP route (request, response, errors).
* `keeper-contract-v1.json` - the same route list plus JSON Schemas (draft 2020-12).
* `tests/contract/` - the black-box suite that enforces this document.

This is the public surface of the plangraph keeper: the HTTP API, the
authentication model, the item state machine, the event record and its hash
chain, the batch file format and the snapshot. Any keeper implementation
(SQLite today, then Postgres, then a Rust rewrite) is conformant when
`tests/contract/` passes against it. Storage is not part of the contract.

Where the current implementation does something this document does not
promise, the behaviour is listed under **Unspecified** (section 11). A client
must not rely on it and a new implementation need not copy it.

## 1. Transport

* HTTP/1.1, JSON in and out, paths under `/v1/`. Unknown path: `404 not_found`.
  A known verb with the wrong method: `405 method_not_allowed`.
* Reads are `GET`, writes are `POST` with a JSON **object** body (the two
  create verbs also accept a bare JSON array of items). A body that is not
  valid UTF-8 JSON, or not an object, is `400 validation`.
* Request bodies are capped at 1 MiB (1048576 bytes): `413 too_large`.
* Responses are `application/json; charset=utf-8`, except text forms below.
* `?fmt=text` asks for a compact human form (`text/plain; charset=utf-8`) on
  any route. It is the **default** for `ready`, `show`, `blocked`, `alerts`,
  `aliases`, `changelog` and `audit-meta`; machine clients pass `fmt=json`
  on those. The text layout is unspecified. All other routes answer JSON
  unless `fmt=text` is given.
* The authenticated principal always comes from the token. `actor`, `role`
  and similar fields in a body are ignored.
* `GET /v1/health` is the only open route: `200 {"status":"ok"}`.
* Timestamps are UTC ISO-8601 with second resolution and a `Z` suffix
  (`2030-01-31T12:00:00Z`). Keys are `project:ID`.

### Error body and status mapping

Every refusal is `{"error": "<code>", "msg": "<text>"}` (exactly those two
keys). The `error` code is normative; `msg` is non-empty free text whose wording
is not part of the contract (clients and tests must not match on it, except
where a section below quotes a stable fragment such as `op <n> <op> <key>`). A
`msg` never contains a token or a stack trace.

| code | HTTP | meaning |
| --- | --- | --- |
| `forbidden` | 403 (401 when authentication fails) | role, scope or ownership refusal; missing/unknown/revoked token |
| `not_found` | 404 | unknown item, route or principal |
| `method_not_allowed` | 405 | verb exists, wrong method |
| `too_large` | 413 | body over 1 MiB |
| `conflict` | 409 | duplicate key, lost race, already claimed, active dependents |
| `locked` | 409 | the item is DONE or cancelled (section 5) |
| `epoch_stale` | 409 | the request carried an old epoch |
| `same_principal`, `same_family` | 409 | review independence rule |
| `lease_held` | 409 | reserved (mapped, never produced in 1.0) |
| `validation` | 400 | malformed or out-of-range input, unknown field |
| `bad_state` | 400 | the item's state does not allow the verb |
| `evidence_missing`, `evidence_unverified` | 400 | evidence rules (section 5) |
| `family_unknown` | 400 | a reviewer or claimant has no family |
| `cycle` | 400 | the change would create a dependency cycle |
| `internal` | 500 | unexpected failure, no details |

Malformed client input must never produce `500`.

## 2. Authentication and principals

* `Authorization: Bearer <token>`; the scheme word is case-sensitive. A
  missing header, an unknown token, a revoked principal or a revoked run
  token is `401 {"error":"forbidden"}`.
* A **principal** has a `name`, a `role`, a list of `projects` and an optional
  `family`. Families are case-folded (`famA` reads back as `fama`); a blank
  family means "unknown" and fails closed wherever a family is compared.
  Family text must match `[A-Za-z0-9._-]+`.
* Roles: `router` (administrator of the plan, every project), `l1` (manages
  its own projects), `worker` (does scoped work), `reviewer` (reviews),
  `spawner` (records runs), `reader` (read only), `system` (the reaper).
* Tokens are random, shown once at creation, and only their SHA-256 is stored.
  Principals are created and revoked **outside HTTP**, with the local admin
  CLI (`pg admin init --router NAME`, `pg admin add-principal NAME --role R
  [--projects a,b] [--family F]`, `pg admin revoke NAME`). A name may not
  start with `run:`; a duplicate name is refused.
* `GET /v1/whoami` returns `{name, role, projects, family}` of the token.
* Every verb re-checks the principal's stored row, so a revoked or re-scoped
  principal stops acting immediately.

### Role matrix (writes)

| verb | who may call it |
| --- | --- |
| `create_items` | router (any project); l1 (own projects, no cross-project deps, no `review_tier: none`, no tier below the default, no cross-project `under`) |
| `import_items`, `set_priority`, `add_evidence_router`, `waive_second_family`, `close_on_decision`, `set_legacy_state`, `apply_ops`, `migrate_descriptions`, `rehome`, `answer`, `set_kind`, `withdraw_review`, `reopen` | router |
| `edit_definition`, `cancel` | router; l1 of the item's project (see state machine for limits) |
| `claim` | l1 or worker scoped to the item's project |
| `delegate`, `heartbeat`, `submit`, `add_evidence` | the claimant, or the delegate (`delegate` itself: claimant only) |
| `release` | claimant or router |
| `verdict` | reviewer scoped to the project |
| `record_run` | spawner |
| `mint_run_token` | router, or l1 of the item's project |
| `replay` | a run token only |
| `reap` | system (no HTTP token of another role may call it) |

Reads: any authenticated principal, except `digest` (router) and `audit-meta`
(router, or l1 limited to its own projects).

### Run tokens

A run token (`pgr1_...`) is minted with `mint_run_token {key, role, run_id}`
(`role` = `writer` or `reviewer`). The run must exist in the keeper's run
record and mention the item. A writer token needs the item `claimed` with a
live lease; a reviewer token needs it `in_review`. It authenticates as
principal `run:<run_id>` (role `worker` for a writer, `reviewer` for a
reviewer), is bound to one key and one epoch, and may call only `heartbeat`,
`add_evidence`, `submit` (writer) or `verdict` (reviewer) on that item, plus
`replay`. Everything else, including reads, is `403`. The token expires at the
lease (writer, capped at 2 h per mint and 24 h in total, extended by heartbeat)
or after the 4 h review window, and is revoked when the item moves on: a new
claim, release, expiry, cancel, close, review result, or (writer) submit.
At most 16 live tokens per item. A use after revocation is `401` or `403`.

## 3. Item model

Fields of an item as returned by `ready`/`snapshot` (all present; nullable
ones are `null` when unset): `key`, `project`, `id`, `title`, `description`,
`kind`, `track`, `owner`, `state`, `priority` (int, higher first), `epoch`,
`round`, `claimed_by`, `delegate`, `lease_until`, `reviewer`, `depends_on`
(keys), `done_criteria`, `evidence_kinds`, `operator` (bool), `review_tier`,
`review_waiver`, `estimate_h`, `estimate_src`, `host`, `wait`, `note`,
`corrects`, `fixes`,
plus derived `created_at`, `done_at`, `reviewed`, `merged`.

* `kind`: `lane`, `gate`, `goal`, `question`, `decision`, `operator_task`.
  The last three wait on the operator. Unknown extra fields on create are
  folded into `note`.
* `under` (create-only, optional): the key of an **existing** gate/goal item.
  `create_items` creates the item and, in the same transaction, the edge
  `gate -> new` (`gate.depends_on` new), so the new item reaches a goal without
  a later edit; the response gains `attached: [{key, under}]` and the gate gets
  one `item_defined {fields: ["depends_on"]}` event plus an `updated_at` touch.
  The gate must exist (`not_found` — a key created earlier in the same batch
  does not), the caller must manage its project (`forbidden`), a non-router only
  attaches inside the new item's own project (`forbidden`), and a `done` /
  `cancelled` gate is `bad_state`. The candidate graph (the gate with the new
  item among its dependencies) is validated before any write, so a cycle or an
  unreachable item is refused atomically and nothing is created. `under` is
  refused by `import_items` and is not an `edit_definition` field
  (`validation`); it is a known field, so it never folds into `note`.
* `review_tier`: `standard` (default; `high` when the id, title or track
  contains a risky word or the project is `plangraph`), `high`, `none`.
* `evidence_kinds`: subset of `decision`, `commit+ci`, `run-record`,
  `semaphore-task`, or `["any"]`. `commit` and `error` exist as router-only /
  never-verified kinds. A `none` item must list exactly `["decision"]` or
  `["decision","commit"]`.
* Graph rules (checked on create/edit/cancel/apply): no cycle, no unknown or
  self dependency, no dependency on a cancelled item, at least one `goal`,
  and every open item reaches a goal.
* `epoch` starts at 0 and increases by one per claim; `round` counts
  submissions of the current epoch.
* Caps: title 200, description 2000, note 2000, done_criteria 600, 500 items
  per call, 200 dependencies per item. Text that looks like a credential pair
  next to a credential word is refused (`validation`).

## 4. Evidence

`add_evidence {key, epoch, kind, ref}` stores a row `{kind, ref, verified,
detail, at}`. `verified` is decided by the keeper, never by the caller.
Rows of kind `decision` verify when `ref.id` (`D-<n>`) is a recorded
decision. An unverified row is stored (`200`, `verified:false`) and counts
for nothing. `commit` evidence is router-only (`add_evidence_router`).
`submit` needs, for each required kind, at least one **verified** row of the
current epoch (`["any"]`: any verified row); otherwise `evidence_missing`
(none of that kind) or `evidence_unverified`.

## 5. State machine

States: `open`, `claimed`, `in_review`, `done`, `cancelled`.

| from | to | verb (who) | guard |
| --- | --- | --- | --- |
| open | claimed | `claim` (l1, worker) | all dependencies `done`; epoch +1; lease 60..86400 s (default 7200) |
| claimed | claimed | `heartbeat`, `delegate`, `add_evidence` | claimant/delegate, current epoch; heartbeat only extends the lease |
| claimed | open | `release` (claimant, router); lease expiry (system) | current epoch |
| claimed | in_review | `submit` | claimant/delegate, evidence rules (section 4); round +1 |
| in_review | claimed | `verdict ok:false` (reviewer) | fresh lease, same epoch |
| in_review | open | second consecutive `verdict ok:false` in one epoch | holder cleared |
| in_review | claimed | `withdraw_review` (router) | decision names the key in `Closes:` |
| in_review | claimed | `reopen` (router) | recorded decision, reason |
| in_review | done | `verdict ok:true` | see review rules |
| claimed, in_review | done | `close_on_decision` (router) | tier `none`, decision evidence, hashed batch file |
| open, claimed, in_review | done | `answer` (router) | kind `question`/`decision`, decision names key |
| open | claimed, in_review | `set_legacy_state` (router) | epoch +1 |
| open, claimed, in_review | cancelled | `cancel` | router, or l1 of the project when no active item depends on it |
| open | cancelled (+ new key) | `rehome` (router) | an alias `old -> new` is recorded |

**Review rules.** A passing verdict needs a reviewer who is not the claimant
or delegate, is scoped to the project, and whose family is known and differs
from the claimant's (and delegate's): else `same_principal`, `same_family` or
`family_unknown`. `standard`: one passing verdict makes the item `done`
(`reviews_passed: 1`). `high`: the first verdict leaves it `in_review`
(`reviews_passed: 1`); the second must come from a different principal **and**
a different family (`same_principal`/`same_family`) unless the router recorded
`waive_second_family` for it, and then it is `done`. `none`: verdicts are
`bad_state`; only `close_on_decision` or `answer` ends it. Verdicts are tied to
the submission round; a change request voids earlier ones. `verdict` needs
`ok` to be a JSON boolean.

**Immutability.** `done` and `cancelled` are terminal. Every mutating verb on
such an item (claim, heartbeat, release, delegate, submit, evidence, verdict,
cancel, edit, priority, ...) is refused `409 locked` and appends no event.
Creating an existing key is `locked` (terminal) or `conflict` (live). Other
items may depend on a `done` item.

**Leases.** `lease_until` is set by claim and extended by heartbeat; the
reaper (every `reap_interval`, default 60 s) returns expired claims to `open`
and flags reviews open longer than 4 h (`review_overdue` finding).

## 6. Events and the hash chain

Every state change appends exactly one record per effect to an append-only log.

```json
{"seq": 7, "ts": "2030-01-31T12:00:00Z", "actor": "worker1", "role": "worker",
 "model": null, "key": "demo:A", "type": "claimed",
 "payload": {"epoch": 1, "claimed_by": "worker1", "lease_until": "2030-01-31T14:00:00Z"},
 "prev_hash": "<64 hex or empty for seq 1>", "hash": "<64 hex>"}
```

* `seq` starts at 1 and has no gaps. `actor`/`role` are the authenticated
  principal (`run:<id>` for run tokens). `model` is reserved (`null`). `key` is
  `null` for keeper-level events (`outage_recorded`).
* **Hash rule:** `hash = sha256_hex(prev_hash + canonical(body))` where `body`
  is the object `{ts, actor, role, model, key, type, payload}` and
  `canonical` is JSON with keys sorted, separators `,` and `:` (no
  whitespace) and non-ASCII escaped (`ensure_ascii`). `prev_hash` is the
  previous record's `hash`, and `""` for the first record.
* The log is append-only and tamper-evident: any edit, delete or reorder
  breaks a later `prev_hash` or `hash`. `pg admin verify` (local) recomputes
  the chain and the cached item state and exits `0` ("chain and cache
  integrity ok") or `2` listing `prev_hash_mismatch` / `hash_mismatch` / cache
  problems.
* Event types: `item_created`, `item_defined`, `claimed`, `delegated`,
  `released`, `lease_expired`, `evidence_added`, `submitted`,
  `changes_requested`, `review_verdict`, `review_passed`,
  `closed_on_decision`, `answered`, `cancelled`, `priority_set`,
  `waiver_set`, `legacy_state_set`, `run_recorded`, `token_minted`,
  `outage_recorded`, `review_withdrawn`, `kind_set`, `item_reopened`, `heartbeat` (reserved).

Events per successful verb: `create_items`/`import_items` one `item_created`
per item (a `create_items` item with `under` adds one `item_defined`
`{fields: ["depends_on"]}` on the gate, in the same transaction; legacy imports
with state `claimed`/`in_review` add `claimed`
(+`submitted`)); `edit_definition` one `item_defined` (+`priority_set` if it
carried `priority`); `set_priority` `priority_set`; `claim` `claimed`;
`delegate` `delegated`; **`heartbeat` none**; `release` `released`;
`add_evidence[_router]` `evidence_added`; `submit` `submitted`; `verdict
ok:false` `changes_requested`; passing verdict `review_verdict` then (when
done) `review_passed`; `cancel` `cancelled`; `waive_second_family`
`waiver_set`; `close_on_decision` `closed_on_decision` (`decision_ids`,
`batch_ref`, `sha256`); `answer` `answered`; `set_legacy_state`
`legacy_state_set`; `withdraw_review` `review_withdrawn`; `reopen`
`item_reopened`; `set_kind` `kind_set`; `rehome` `item_created` (new, `rehomed_from`) + one
`item_defined` per re-pointed dependent + `cancelled` (old);
`record_run` `run_recorded`; `mint_run_token` `token_minted` (never the token);
`apply_ops` the events of each op, in order, in one transaction. Refused
requests, reads and duplicates append nothing.

**What HTTP exposes.** Events are not served. `GET /v1/snapshot` carries the
chain head: `head` (hash of the last record, `null` when empty) and
`head_seq`; `GET /v1/changelog?since=<seq|date>` returns a derived feed
(`seq`, `ts`, `type`, `key`, `by`) of creations, closes, answers, cancels and
restarts. A conformance run verifies the full chain from an event export (one
JSON record per line in the shape above): see section 10.

## 7. Batch files

A batch is a YAML file replayed by the router through `tools/apply_batch.py`.

```yaml
batch: example-batch          # label
principal: router             # must equal the token's whoami name; role must be router
source: plan/example.md       # file whose bytes are pinned (relative to the batch file's real dir)
source_sha256: <64 hex>       # sha256 of `source`; mismatch -> refuse before any call
ops:                          # applied in order
  - {op: create, item: {project: demo, id: A, kind: lane, title: "..."}}   # item: mapping or list
  - {op: edit, key: "demo:A", fields: {title: "..."}}
  - {op: priority, key: "demo:A", priority: 3}
  - {op: legacy_state, key: "demo:A", state: claimed, note: "..."}          # claimed | in_review
  - {op: add_evidence, key: "demo:A", kind: decision, ref: {id: D-1}, repo: ...}
  - {op: close_on_decision, key: "demo:A"}
  - {op: cancel, key: "demo:A", reason: "..."}
  - {op: rehome, key: "demo:A", new_project: other, host: box1}
  - {op: set_kind, key: "demo:A", kind: question}                           # or operator_task
```

* Only the keys shown are allowed (per op and at top level); duplicate YAML
  keys, unknown ops, unknown fields and bad types are refused locally before
  any call. Keys must look like `project:ID`.
* Op to verb: `create`->`create_items`, `edit`->`edit_definition`,
  `legacy_state`->`set_legacy_state`, `add_evidence`->`add_evidence_router`,
  `close_on_decision`, `cancel`, `priority`->`set_priority`, `rehome`,
  `set_kind`. Router only.
* **Default mode: one `apply_ops` call** `{ops, batch_ref}`, all or nothing in
  one transaction (max 500 ops). Later ops see earlier ones; the whole graph
  is validated once at the end. Any refusal rolls everything back and names
  the op: `op <n> <op> <key>: <code> <message>` (`n` is 1-based). A refusal
  from the final graph check with no owning op is `op - validate: ...`.
  `close_on_decision` uses `batch_ref = "<absolute source path>@<sha256>"`; the
  file must resolve inside a configured batch root, and its digest must match.
* `--per-op` replays one verb at a time, stops at the first refusal and
  prints `resume with --from <n>`; `--from N` resumes at op N (1..ops).
  `--dry-run` validates and prints `plan <n> <op> <key>`, calls nothing.
  `--queue-line L --queue-file F` appends `L` to `F` only after a complete
  `applied N of N` run.
* CLI output: `acting as <name> (<role>)`, then `ok <n> <op> <key>` per op (or
  one error block), then `summary: ... (applied K of N[, skipped M (before
  --from)])`. Exit `0` success, `1` refusal, `2` bad arguments or token file.
  The pin mismatch line contains `sha256 mismatch`; a principal or role
  mismatch prints `principal mismatch` and calls nothing else.
* `apply_ops` over HTTP: `{"applied": N, "results": [...]}`; `results` holds
  one object per op in op order: `{"created": [keys]}` for `create`, and an
  object with the affected `key` for every other op; a bad op list is
  `400 validation`; the failing op's code and status are returned with the
  `op <n> ...` message; a non-router is `403`.

Directory import (`pg admin import DIR --as ROUTER`) and export
(`pg admin export DIR`) move the same item definitions as YAML files for
bootstrap; the format is the item fields of section 3, one mapping per item.

## 8. Snapshot

`GET /v1/snapshot` returns `{generated, head, head_seq, items[], aliases{old:
new}, activity[], decision_text{}, progress{}}`. `items[]` carry the item
fields of section 3 plus `reviews` (`[{reviewer, family}]` of the current
round), `evidence` (`[{kind, ref, verified, at}]`) and `answer`
(`{text, decision_id}` or `null`). The keeper also keeps a file copy
(`plan.snapshot.json`, same body) up to date: it is rewritten atomically
(temp file then rename) after every committed write and by the reaper.
Optionally (a signing key configured) a detached signature
`<snapshot>.sig` is written first: base64 of the 64-byte ed25519 signature
over `b"plangraph-snapshot-v1\n" + <exact file bytes>`. A reader holding the
public key refuses an unsigned, tampered or wrongly signed copy
(`pg admin verify-snapshot`).

## 9. Reads at a glance

`ready` (open items with every dependency `done`, sorted by priority desc,
number of dependents desc, `estimate_h`, key), `show`, `blocked`
(`{key: [unmet keys]}`), `waves` (layers of keys, wave 0 ready now),
`critical_path` (`{keys, hours}`), `percent` (a number 0..100, `by` =
`estimate` | `count`, cancelled excluded), `alerts`, `digest`, `snapshot`,
`aliases`, `needs_you`, `answers`, `progress`, `changelog`, `audit-meta`,
`whoami`, `health`. Shapes are in the routes file and the JSON schemas.

## 10. Conformance suite

`python3 -m pytest -q tests/contract` (also collected by `tests/`). Without
configuration it starts the Python keeper as a subprocess on a temp directory
and a free localhost port, seeded through the admin CLI. To test another
keeper set:

| variable | meaning |
| --- | --- |
| `PG_CONTRACT_URL` | base URL; switches the suite to external mode |
| `PG_CONTRACT_TOKEN_<NAME>` | token per principal (`ROUTER`, `LEAD`, `LEAD_OTHER`, `WORKER1`, `WORKER2`, `REVIEWER1`, `REVIEWER2`, `REVIEWER_SAME`, `REVIEWER_NOFAM`, `READER`, `SPAWNER`) |
| `PG_CONTRACT_CAPS` | comma list of server-side fixtures present: `decisions`, `runs`, `batch`, `admin` |
| `PG_CONTRACT_BATCH_DIR`, `PG_CONTRACT_RUNS_DIR`, `PG_CONTRACT_ADMIN_CONFIG` | paths for `batch`, `runs`, `admin` |
| `PG_CONTRACT_EVENTS_JSONL` | event export (JSON lines, section 6 shape) for the chain check |

Principals: `router` (router); `lead` (l1, project `demo`, family `famA`);
`lead_other` (l1, project `other`); `worker1`, `worker2` (worker, `demo`,
`famB`); `reviewer1` (reviewer, `demo`, `famC`); `reviewer2` (`famR`);
`reviewer_same` (`famB`); `reviewer_nofam` (no family); `reader` (reader,
`famD`); `spawner` (spawner, `demo`). The fixture decisions file must hold
`| D-1 | demo | A plain recorded decision. |`, `| D-2 | demo | Closes:
demo:CLOSE-1 demo:ANS-1 demo:WD-1 |` and `| D-3 | demo | Waives: demo:HIGH-1 |`
(a decision row is `| D-<n> | project | decision text |`; `Closes:`/`Waives:`
tokens count only in the third cell). Tests that need a fixture the keeper
lacks skip. Use a fresh keeper per run: four fixture keys are single-use.

## 11. Unspecified (do not rely on)

* The layout of every `text/plain` form; `?fmt=text` on writes.
* `POST /v1/health` (answers 200 today), `HEAD`/`OPTIONS` (501 today).
* `GET /v1/show` without a key (404 today); `?all=` takes any value as true
  (`all=0` still means all); `ready?mine` compares the principal name to `owner`.
* Numeric coercion of `n`/`lease_s`: numeric strings, floats and booleans are
  truncated by `int()` today; send integers.
* Whether `fmt=json` reads are audited. The audit log is not served.
* `lease_held`, the `heartbeat` event type, and `model` are reserved.
* Header casing other than `Authorization: Bearer`; multiple `Authorization`
  headers; duplicate JSON object keys (last wins today).
* The default lease (7200 s), `reap_interval` and `default_lease` are
  deployment settings; only the 60 s to 86400 s clamp is promised.
* Event `ts` equality inside one second; ordering of `results` beyond op order.
* Out of scope for 1.0: external anchoring of the chain head
  (`pg admin verify-anchor`), the ssh snapshot pull (`pg sync pull`), the MCP
  stdio server, the CLI's own text output, and the web viewer.
