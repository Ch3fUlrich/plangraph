# Keeper on Postgres (RT-P1-KEEPER-PG-CODE, W1)

Scope of W1: a Postgres storage backend next to SQLite, behind one seam, proven by the frozen black-box contract suite
(`tests/contract`) and the storage-facing keeper tests running against a Postgres service container in GitHub Actions.
SQLite stays the default. Not in W1: the design-3 tables/states (done in W2, below) and the SQLite -> Postgres copy tool (W3, below).

## Selecting the backend

* `keeper.json` `"backend": "postgres"` (or env `PG_KEEPER_BACKEND=postgres`) selects Postgres; anything else is SQLite.
  Selection is explicit on purpose: a stray `PG_DSN` must never silently repoint a running SQLite keeper.
* DSN: `keeper.json` `"dsn"`, else env `PG_DSN`. It is never printed, logged or put in an error message.
* Namespace: `keeper.json` `"pg_schema"` (else env `PG_SCHEMA`, else `k_<sha1 of the resolved db path>`). The `db` path still
  exists in the config: it anchors the schema name, the `keeper.alive` outage file and the config-relative files. The derived
  name lets many throw-away keepers (tests, contract runs) share one Postgres server without touching each other.

## Seam

`src/plangraph/keeper/db.py` stays the only way the keeper opens a database. `open_db(path, create=, backend=, dsn=, schema=)`
returns either a `sqlite3.Connection` or a `db_pg.PgConnection`. `PgConnection` is a thin wrapper that presents the small
part of the sqlite3 connection API the store uses (`execute`, `close`, `in_transaction`, rows addressable by index and by
name, `rowcount`), so the 130 existing `conn.execute` call sites stay portable SQL with `?` placeholders.
Dialect differences are handled in exactly these places:

| Difference | Handling |
|---|---|
| `?` placeholders | translated to `%s` by the adapter (quoted literals skipped) |
| `BEGIN IMMEDIATE` | adapter: `BEGIN` + transaction-scoped advisory lock (see Locking) |
| `INSERT OR IGNORE` | portable `ON CONFLICT DO NOTHING` (SQLite >= 3.24 and Postgres) |
| `cursor.lastrowid` | `events.seq` is assigned by the append (last seq + 1, no sequence); `run_tokens.id` via `INSERT ... RETURNING` |
| exceptions | `db.INTEGRITY_ERRORS`, `db.OPERATIONAL_ERRORS`, `db.MALFORMED_ERRORS` tuples cover both drivers |
| `sqlite3.Row` | `PgRow` (index, name, `keys()`, iteration over values) |
| `bool` parameters | converted to `0/1` (no boolean columns exist; SQLite did this implicitly) |
| text ordering | every TEXT column is `COLLATE "C"` so `ORDER BY key` equals SQLite BINARY order |
| JSON columns | stay TEXT holding the same canonical JSON, so event hashes are backend independent |
| triggers | plpgsql functions + row triggers, same names and same `key is locked` / append-only messages, `ERRCODE 23000` |
| authorizer (DDL deny) | adapter statement guard refuses DDL on a keeper connection; real enforcement is a runtime DB role without DDL rights |
| `lock_keeper` flock | session advisory lock on a dedicated connection (one writer across hosts) |

## Locking

SQLite has one writer. Postgres writes use `BEGIN` followed by `pg_advisory_xact_lock(<const>, hashtext(schema))`: all
writers of one keeper schema are serialized exactly like `BEGIN IMMEDIATE`, in READ COMMITTED. The store already re-reads and
guards (`WHERE state=? AND epoch=?`, `_row_guard`) inside the write block, so the lock reproduces the SQLite behaviour
(one winner on concurrent claim, epoch/lease fencing) without SERIALIZABLE retry loops. `lock_timeout` is set to the SQLite
busy timeout (5 s); expiry maps to the same `conflict` refusal as a SQLite busy error.

## Schema and migrations

Migration 8 adds the scoped oversight token and append-only op audit tables
(SQLite schema v14, `OVERSIGHT_DDL`). Migrations 1–7 remain unchanged,
including all five exec ownership columns in Migration 7.

`schema_pg.py` holds a versioned migration list; each migration is one DDL script. Version 1 is the logical equivalent of
SQLite schema v9 (tables, indexes, locking triggers, aliases, run_tokens, outbox_seen). `schema_migrations(version, name,
applied_at)` records what ran. `open_db(create=True)` and `pg admin migrate` apply pending migrations under an advisory
lock; `open_db(create=False)` refuses a database that is behind (a runtime role has no DDL rights). A unit test compares the
SQLite and Postgres table/column lists so the two schemas cannot drift.

## Hash chain

`events.hash` is computed in Python from canonical JSON (sorted keys, no whitespace, ASCII) over text values; nothing in it
depends on the database. `tests/test_keeper_pg_parity.py` replays one operation sequence on SQLite and Postgres and compares
every `(seq, hash)` pair and the head.

## CI

Job `pytest-postgres` (`.github/workflows/tests.yml`): `services: postgres:16`, env `PG_DSN`, `PG_KEEPER_BACKEND=postgres`;
runs `tests/contract` plus the keeper modules that exercise storage. The existing `pytest` job is unchanged.

## Open questions

(Answered conservatively; revisit in W2.)

* Three contract tests read the SQLite file directly (`test_events.py` chain export and tamper check). They cannot run on
  Postgres without editing the frozen suite; the PG job deselects them (`tests/conftest.py`, list unchanged). Answered in W2:
  `tests/test_keeper_http_events.py` is the HTTP-level replacement (no database driver imported). It starts its own keeper
  through the contract harness, reads the chain head from `/v1/snapshot` after each write and requires
  `head == sha256(previous head + canonical(expected event))` (type, actor, role, key, payload; the `ts` is searched in the
  request window), checks that reads, refusals and heartbeats leave the head alone, and runs `admin verify` (the adapter, so
  it is backend independent) for full chain and cache integrity. The keeper still serves no event export over HTTP, so the
  tamper check (forge a row, expect `hash_mismatch`) stays SQLite-only in the frozen suite; on Postgres tampering is refused
  by the triggers (`test_keeper_pg_parity.py::test_triggers_refuse_tampering`,
  `::test_design3_triggers_refuse_tampering`), and `test_keeper_pg_parity.py` compares every `(seq, hash)` pair and the head
  against SQLite. Two storage tests that inspect the SQLite file (`brief_stats` read-only open, `admin init`
  file check) and every test module importing `sqlite3` are likewise left out of the PG job.
* The runtime role must lack DDL rights; creating that role is deployment (RT-P1-PG-DEPLOY), not W1.
* Connection pooling: one connection behind the existing API lock, as with SQLite.

## W2: design-3 tables, states, store verbs

SQLite schema v10 (`schema.DESIGN3_DDL`, `IF NOT EXISTS`, installed by `create=True` and by `db._upgrade` when an older file is
opened) and Postgres migration 2 (`schema_pg`, logical twin: `BIGINT`/`DOUBLE PRECISION`/`TEXT COLLATE "C"`, plpgsql triggers
with the same messages). `test_keeper_pg_unit.py` compares tables, columns, indexes and trigger names over all migrations.
Only `TEXT`/`INTEGER`/`REAL` columns; list-valued columns hold canonical JSON text. Ids of `attempts` and `questions` are
assigned as `MAX(id)+1` inside the write lock (like `run_tokens`), so both backends number rows alike.

| Table | Columns / rules |
|---|---|
| `cards` | `key` PK -> `items.key`; `outcome`, `read_set`, `write_set`, `exports`, `forbid_paths`, `check_cmd`, `check_kind` (`change`/`regression`, default `change`), `check_reads` (JSON list, default `[]`), `smoke_checks`, `heldout_check`, `window_class` (`128k`/`256k`/`1m`), `risk_tags` (JSON), `estimate_tokens`, `updated_at`. Mutable (full replace). |
| `attempts` | `id`, `key`, `n`, `harness`, `model`, `combo`, `class`, `started_at`, `ended_at`, `exit_class` (design 4.5: ok, check_fail, no_change, flaky_check, usage_limit, outage, lease_lost, worker_crash, stall, timeout, rebase_conflict, merge_refused, probe_fail, contract_defect, needs_input, cancelled), `tokens_in`, `tokens_out`, `cost`, `run_id`, `workflow_id`, `log_ref`; `UNIQUE(key, n)`. Append-only (a finished attempt is recorded once). |
| `decisions` | `id` (e.g. `D-1285`), `ts`, `actor`, `kind` (`operator`/`judge`), `text`, `closes`, `waives` (JSON key lists). Append-only. |
| `questions` / `answers` | question: `id`, `key`, `text`, `default_answer`, `deadline`, `risk_tags`, `state` (`open`/`answered`/`timeout`), `created_at`. Answer: `question_id` PK, `ts`, `actor`, `text`, `source` (`answer`/`question_timeout`). `answers` append-only. |
| `budget` | PK `(provider, day)`; `tokens`, `cost`, `meter` (the Claude weekly meter). Upsert of absolute values. |

States: `schema.STATES` = open, ready, claimed, in_review, done, cancelled, running, needs_input, waiting, failed, parked
(`views.STATES` is the same tuple, so `validate` accepts them). `set_state` moves an item among the non-claim states
(open, ready, running, needs_input, waiting, failed, parked); claim, review, done and cancel keep their own verbs, a claimed
or locked item is refused, setting the current state is a no-op without an event. `cancel` now also accepts an item in any
live state (it was open/claimed/in_review).

Events: one new kind `state_set` (`{state, was, reason}`), folded in `events.fold` (state only; `verify_integrity` covers it).
Existing kinds, payloads and hashing are untouched; `test_keeper_pg_parity.py` pins the head of the pre-W2 replay.
The other new tables carry no events (their append-only triggers are the tamper guard; the audit log records every verb).

Store verbs (`store_cards.CardsMixin`, no new HTTP routes): `put_card`, `get_card`, `record_attempt`, `add_decision`,
`ask_question`, `answer_question`, `set_budget`, `set_state`. Roles: cards, attempts and states by router, system or the l1 of
the item's project; decisions and answers by router only; budget by router or system; questions by router, system or a
l1/worker/spawner of the item's project. Questions follow design 4.8: a question with a default needs a deadline;
`source='question_timeout'` (router or system) applies the stored default once the deadline has passed; a question tagged
auth, secrets, money, live-step or public-release cannot carry a default and never times out.

## Copy tool (W3)

`pg --config keeper.json admin copy-to-pg (--dry-run | --apply) [--src <sqlite file>]` copies a SQLite keeper into an
empty Postgres keeper (`src/plangraph/keeper/copy_to_pg.py`). Exactly one of `--dry-run` / `--apply` is required; `--src`
defaults to the configured `db`. The target is always Postgres: DSN and schema come from `keeper.json` (`dsn`, `pg_schema`)
or env `PG_DSN` / `PG_SCHEMA`; the DSN is never printed or put in an error message.

* `--dry-run` reads the source only (opened `mode=ro`): per-table row counts, the events chain head, and a full chain
  check. A broken source chain exits 2. The target is not contacted.
* `--apply` creates or migrates the target schema (`open_db(create=True)`), then in ONE transaction copies every table
  verbatim in primary-key order (events, items, evidence, edges, principals, aliases, run_tokens, outbox_seen, audit_log,
  audit_findings, then cards, attempts, decisions, questions, answers, budget) with parameterised inserts, ids and `seq`
  preserved, values unchanged, so event hashes stay byte-identical. Before commit it checks per-table row counts, that the
  target chain head equals the source head and that the copied chain verifies; any mismatch rolls everything back and exits 2.
  The key-lock triggers are disabled for the copy only (events are loaded before the items and evidence they lock), inside
  the same transaction, and re-enabled and checked before the commit; this needs the table owner (the role that migrates).
  Identity sequences are advanced past the copied maximum so the first keeper write after the copy does not collide.
* Emptiness rule: the target must hold no rows in any keeper table (the append-only triggers make a merge impossible); a
  second `--apply` is refused with `target is not empty`.
* Copy a snapshot, never a live database: stop the keeper (or copy the file with the SQLite backup API) first. Writes made
  to the source after the copy are not carried over.
* The report is plain text (table, source rows, target rows, head); it holds no paths, hosts or secrets.
