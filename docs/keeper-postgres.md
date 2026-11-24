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

Migration 16 creates `event_audience` for the scoped replay feed. Run
`pg --config CONFIG admin migrate` with the migration role before restarting
the keeper. The runtime role needs SELECT and INSERT on `event_audience`.
The pre-audience database test checks `sqlite_master` and PostgreSQL
`pg_constraint` before and after the upgrade. The catalog checks confirm
that `events.type` is text with no CHECK. The test then appends and replays
all four new grant and question kinds. `EVENT_TYPES` is the application
fold vocabulary.

Migration 18 installs `landing_stages` with `pg admin migrate` using the schema
owner's connection. SQLite installs the same table on open. Each completed stage
has a repository, batch key, name, positive attempt number, exact Git head, and
start and end times in UTC Unix milliseconds. Producers use
`landing_stage_insert.sql` with the same identity on delivery retry. The first
record wins. A new execution of that stage uses a new attempt number. The read
query in `landing_stage_list.sql` returns the batch's stages in start-time order.

Migration 19 installs the keeper landing journal. A configured lander writes the
train's frozen manifest, landing mode and receipt inputs before a PR merge or tested fast-forward.
`merge_batch.py settle` discovers unfinished trains and verifies the landed commit
against main. For tested fast-forward, the exact tested head must be an ancestor
of main. Settlement records the checked PR outcomes before advancing the cursor
and excludes moved PRs from cleanup. Recovery uses the keeper merge lease for that repo.
A new push can replace the prepared manifest while the train is open.
Set `PG_LANDING_HOSTS` to the host registry for the three repository checkouts.
Each host entry supplies `PG_LANDING_BUILD_STAMP` and, for repair,
`PG_LANDING_SYNC_EXECUTABLE` as absolute paths. The executable receives the
repository root and merged SHA. The stamp names the build head and installed
binary paths with SHA-256 digests. Settlement persists all three receipts
before advancing past host verification.
To stop landed native writers during host verification, set
`PG_LANDING_WRITER_RECORDS` in each host entry to an owner-local JSON array of
candidate record paths. `pg-spawnd writer-stop MERGED_SHA RECORD...` reports
which candidates it stopped or kept. `pg-spawnd writer-resume RECORD` resumes
the preserved session from its original request and worktree.

Migration 20 stores individual GitHub Actions job timings. The lander imports the
exact run and attempt with `plangraph landing ci-import OWNER/REPO BATCH HEAD RUN ATTEMPT`.
The writer and delta reviewer use `PG_LANDING_REPO` for the repository identity.
Train push, GO observation, merge and host sync share the train's keeper batch key.
Use `plangraph landing report OWNER/REPO BATCH` for the train card and milestone
timestamps, or `plangraph landing report OWNER/REPO --day YYYY-MM-DD` for the slowest
stage completed that UTC day. The GO request includes the elapsed stage summary.
Import the green main Actions attempt under the same batch key to populate
the exact merged-head completion and the elapsed time from the folded parts' ACCEPT.

Migration 21 records dependency claims against accepted open-train heads. Migration 22 indexes
keeper incident events by item and sequence. Migration 23 records card effort decisions, and
migration 24 stores terminal spawn attempt outcomes. SQLite installs the claim and incident changes in schemas v22 and v23
and both of the remaining changes in schema v24. PostgreSQL migration 25 and SQLite v25 add the claim definition event index.

For a batch that advances the keeper schema, copy the GO request's standalone
`MIGRATION <version>` field into the GO decision for the exact batch head.
The landing command requires that field on the same GO statement.

For the merged-unit restart, the access owner provisions a regular file owned by
the deploy user with mode `0600`. It holds the migration role's DSN for the same
database as the keeper configuration. The role needs schema ownership or its
equivalent DDL and migration-table permissions. Creating a missing schema still
requires database-level permission.

The file accepts a raw PostgreSQL DSN or one `PG_DSN=<value>` line. The value
can be wrapped in matching single or double quotes.

On the keeper host, point the effective unit at the candidate checkout and use
its runtime configuration:

    plangraph keeper-restart CHECKOUT KEEPER_CONFIG MIGRATION_DSN_FILE

The command reloads the user systemd manager before reading the unit. It checks
the unit's working directory and expects a direct Python `-m plangraph.keeper.daemon
--config PATH` command. The admin commands use that interpreter and the user
manager's environment, including the unit's `Environment`, `EnvironmentFile`
and `UnsetEnvironment` settings.

The command runs `pg admin migrate --dsn-file FILE --dry-run` to validate the
file without connecting. It then runs the migration and opens
the candidate schema with the runtime role through `pg admin check-schema`.
It requests the restart only after all checks and the migration succeed. A reload, migration
or runtime schema-check failure leaves the existing keeper process running.

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

## Keeper planning baseline

Install the `pg-telemetry` binary alongside `plangraph`, then run
`plangraph baseline --keeper-only [--since RFC3339]` on the keeper host.
Set `PG_TELEMETRY_DSN_FILE` to the private reader DSN file and `PG_SCHEMA`
to the keeper schema if it differs from `keeper`. The reader needs SELECT
on the report columns of items, edges and events. The reports share one
repeatable-read, read-only transaction and a single timestamp. The optional
window applies to event counts. Readiness and fanout use the full graph.

The PostgreSQL fixture runs with
`cargo test -p pg-telemetry -- --ignored` from `rust`; a plain `cargo test` compiles it and skips it.
Set `PG_TELEMETRY_TEST_DSN` to a disposable PostgreSQL database for fixture
setup. The fixture switches to a restricted reader to run the report.
