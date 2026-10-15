# Keeper on Postgres (RT-P1-KEEPER-PG-CODE, W1)

Scope of W1: a Postgres storage backend next to SQLite, behind one seam, proven by the frozen black-box contract suite
(`tests/contract`) and the storage-facing keeper tests running against a Postgres service container in GitHub Actions.
SQLite stays the default. Not in W1: the design-3 tables/states (cards, attempts, decisions, questions/answers, budget;
needs_input/waiting/failed/parked) and the SQLite -> Postgres copy tool (W2/W3).

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
  Postgres without editing the frozen suite; the PG job deselects them and `test_keeper_pg_parity.py` covers the same
  chain check through the adapter.
* The runtime role must lack DDL rights; creating that role is deployment (RT-P1-PG-DEPLOY), not W1.
* Connection pooling: one connection behind the existing API lock, as with SQLite.
