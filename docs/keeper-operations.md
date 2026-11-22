# Keeper operations

See [Keeper](KEEPER.md) for the storage model and verb contracts.

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

The verb only returns data; the CLI writes the files. One `<project>.BATCH.yaml` per project **that has findings** (silent projects write nothing), plus its sibling note, and the directory is created if missing; the written paths go to stderr so stdout stays the text (or `--json`, which still writes the files). The YAML is `docs/BATCH.md` shape (`batch: audit-meta-<project>`, `principal: router`, `source: <project>.BATCH.md`, `source_sha256`, `ops:`), and `source_sha256` is computed over the note's bytes as they are written, so `python3 tools/apply_batch.py DIR/P.BATCH.yaml
--dry-run` (validation and digest check, no HTTP) accepts it — which the tests prove by importing `tools/apply_batch.py`.
Project names are made filesystem-safe (`[^A-Za-z0-9._-]` → `_`).

Exactly one `op: edit` per item, listing only the missing fields, each value an empty string: `apply_batch`'s validator accepts `""` (checked), so the stub parses as-is and the emptiness is the point — nothing here proposes a value. Two YAML comments precede each op: the item's `key | title | state` + the missing fields, and its current `description=… | estimate_h=… | done_criteria=… | owner=…`. The note file explains all of that and two caveats: `principal: router` is a placeholder (an applier needs a router token of that name; an l1 has no batch door and edits with `pg define --edit`), and an item listed as `state claimed` cannot be edited until it is released, because `edit_definition` only accepts `open` items.

## Backup retention (PG-BACKUP-RETENTION)

The keeper writes no backups itself; operators leave ad-hoc copies (`backup-pre-*.db`, `keeper.db.pre-*`, `keeper.db.bak-*`, `pre-*.db`, `keeper-pre-*.db`) in the keeper data dir, and they pile up. `pg admin prune-backups` trims them. It needs no keeper config and has no default directory.

    pg admin prune-backups --dir DIR [--keep 10] [--daily-days 7] [--apply]

- Policy: keep the newest `--keep` files by mtime, plus the newest file of each of the last `--daily-days` UTC calendar days; delete the rest. Files with a future mtime are kept.
- **Dry run is the default**: it lists what would go, bytes freed and `du before` / `du after (projected)`, and deletes nothing. Only `--apply` unlinks. Exit 0 ok, 2 bad arguments or directory (`/`, empty, missing), 1 if any delete failed.
- Never touched, whatever the name: `keeper.db`, `keeper.json` (and `keeper.json.*`), `*.token`, `*-wal`, `*-shm`, `*.lock`, `*.pid`, `*.alive`, `*.log`, `plan.snapshot*`, `md-sync*`, symlinks, sub-directories (not entered; no recursion) and any name outside the explicit allow-list in `backup_retention.py`.
- Opt-in keeper hook (default off), in `keeper.json`: `"backup_prune": {"enabled": false, "dir": "<path>", "keep": 10, "daily_days": 7, "interval_s": 21600}`. When `enabled`, the daemon prunes (apply) once after startup and then every `interval_s` on a daemon thread that stops on shutdown; errors go to stderr and never stop the keeper. `enabled` without `dir` is a config error. If startup fails before the shutdown thread exists, `daemon.run()` independently attempts to stop the reaper and close the socket under `suppress(Exception)`, preserving the original startup error even if cleanup raises.
- Alternative to the hook: run `pg admin prune-backups --dir DIR --apply` from a Semaphore playbook or cron job.

For a Postgres schema advance, use the [migration-first keeper restart](keeper-postgres.md#schema-and-migrations) on the keeper host.

## Run-token write receipts

The `pg` CLI uses the native `plangraph` client for run-token heartbeat, evidence, and submit
writes. The client saves a fresh invocation ID and one file per write before sending. Set
`PG_OUTBOX` to the run's original JSONL path to use the adjacent `.d` directory and import
its entries in order. With no override, the client uses its local state directory and a queue
bound to the run credential.

`pg replay` resumes the same queue with the original run credential. The keeper authenticates
before reading request bodies. A verified revoked run credential only permits exact receipt
readback on the receipt routes. The keeper commits the mutation and its result together, so the
same invocation can recover the result after a lost reply
even if a submit revoked its token. A submit carries its source review round and waits for
earlier evidence in the same claim to be verified, superseded by later verified evidence of
the same kind, or explicitly resolved. An imported entry with no historical
outcome stays queued for routing recovery. A router can record a disposition with
`pg replay --resolve-entry INVOCATION --decision D-ID` using the run's `PG_OUTBOX`.

For rollout, apply Postgres Migration 15 (`keeper write receipts`) with `pg admin migrate`,
restart the keeper, and deploy the matching native `plangraph` client. The migration imports
known legacy outcomes. SQLite installs the same receipt store when the keeper opens the database.

### Seat verdict receipts

The spawner registers the item, claim epoch, review round, verified head, and seat run before
launch. The reviewer binds its boolean verdict and reason once. Direct delivery and replay
both check the admission and the payload inside the verdict transaction. The original
reviewer run token can read its settled verdict through direct delivery or replay after
revocation. Every entry in such a replay batch must already be settled for that token.
Outside run-token replay, only reviewer principals may submit an all-verdict replay batch.
A new round requires a new seat binding even when the head SHA is unchanged. The keeper stores these records in
`op_receipts` alongside run-token write receipts.

## Placement rules migration-gates
Migration 11 requires operator GO naming the reviewed SHA and scope, verified
backup and disposable-schema rehearsal before application through the migrate role.
Verify runtime DML-only grants, approve API restart, and rehearse rollback first.
Writers have no live authority; tests use only fixture-owned temporary schemas.
Keep application writers stopped during rollback and restore: fixed tables and
indexes become `*_retired_v11` under migration and exclusive table locks.
Preserve audit rows; reapplication requires an explicit restore plan.
SQLite rollback retains tables; C4 owns forward audit events, not item events.
Mandatory ephemeral PostgreSQL CI must pass before merge; storage is not closure. `quota_alarms.py` (systemd timer, deploy/systemd) reads gateway quota snapshots (sqlite opened read-only via `QUOTA_SNAPSHOT_DB`, else `docker exec`) and sends one routing line per 50/75/90 % threshold per window, deduped in a pruned state file with poll checkpoints and last null-reset observations to recover refills across downtime; timestamp formats are normalized and read-only database paths are URI-escaped; a window with no snapshot for 3 h, or no fresh snapshot at all, sends a `quota-alarm-stale` or `quota-alarm-blind` line (daily while it lasts) instead of exiting silent. Quota alarm polls skip malformed snapshot rows independently and include their count as `malformed=N` in the stale/blind notice; healthy windows continue to alarm. Corrupt JSON state is moved to a timestamped `.corrupt-*` sibling, and the poll starts empty with a recovery notice (dry runs leave the file unchanged). Docker reads require an explicit `QUOTA_GATEWAY_CONTAINER`; prefer the read-only `--db` source. The example systemd unit requires a checkout at `%h/code/plangraph` and `/usr/bin/python3` version 3.11 or newer; override WorkingDirectory, PYTHONPATH and ExecStart for a different checkout or virtualenv.

Rule 3.26: merged-close backfill may count a passing spawner cli_reported review
record bound to the merged head, with consistent attempt provenance. Auth/secrets
cards cannot use these low-trust seats; their existing two-seat gate remains.
When reading a diff-bound seat, the keeper requires the dispatch and completion
bindings to agree on `base_sha`, `head_sha`, and `diff_sha256`. The bound head
must also match the completion and the requested head. A caller that supplies
an expected `review_binding` requires an exact match, so a legacy head-only
record can count only when the caller has requested head-based evidence.

[Per-item checkpoint and status notes](checkpoints/). Add new notes there, one file per item, rather than appending here.

`ready` uses the claim guard's merged-work and newer-decision checks. Its JSON
reply separates startable `items`, `needs_reconcile` rows (with `reason`), and
`in_progress` rows (with `prs`, the open PR numbers), and `unknown` rows when
remote facts cannot be verified. Text and CLI print startable rows first, then
separate, indented Needs reconcile, In progress and Unknown sections; only
startable rows retain the top-level `key | ...` line format. `-n` caps only
startable work. `ready --needs-reconcile` prints only dropped rows and their
reconciliation reasons (also available as JSON); it requires the keeper.
Untouched age and superseded-premise context remain informational, matching claim.
Merged trailers, branch topology and open PR heads share a per-repository cached
index (10 seconds), refreshed from origin. All candidate rows are checked;
ready performs no per-item git/gh calls. Failed origin fetches never classify
items from stale local facts. Failed PR-head fetches, missing head commits and
unavailable PR inventories remain unknown. A single visible text WARNING line
and the JSON `warnings` list report each affected project's stale index, reason
and `age_seconds` since the last successful origin fetch (null / age unknown
if none was observed). Cached failures retain their warning and age; they are
retried after 10 seconds. Independently verified decision/merge facts can still
require reconciliation. Unconfigured or unavailable local adapters retain the
existing best-effort behavior. Claim refreshes the index before enforcement;
an unverified index refuses claim with `work index unverified` unless the caller
uses the existing audited force-stale override. Its override, audit and
authorization rules remain unchanged. Ready and claim share merge PR metadata
computed from the refreshed target topology, even when local main lags origin.

`PYTHONPATH=/path/to/plangraph/src /path/to/plangraph/.venv/bin/python3 -m plangraph.keeper.cli sweep-claims` prints exactly `{"paths":[...],"branches":[...]}` for active
claimed/in_review items across every project, using a fresh snapshot and each
item's open lanes. Claim metadata paths are included when present. It requires
an unscoped `sweep` (or `router`) token (`projects=[]`) and refuses unavailable or truncated lane inventories,
malformed metadata, filtered snapshots, or a snapshot changed during collection;
there is no offline fallback. The read-only `sweep` role may call only `whoami`,
`snapshot`, and `show` (claims, runs, and PR links); all write verbs and unrelated
reads fail closed with `403 forbidden`. Hosts can set
`PG_SWEEP_CLAIMS_READER="PYTHONPATH=/path/to/plangraph/src /path/to/plangraph/.venv/bin/python3 -m plangraph.keeper.cli sweep-claims"` directly; see [pg-reap](pg-reap.md).

The sweep reader uses the real `show.run_ids` association written by `record_run`
to read local `runs/<id>/record.json` checkout/cwd/run_worktree metadata, including
completed writer runs whose item is still in review. `sweep-claims --state DIR`
overrides the local state directory; otherwise `PG_SWEEP_STATE` from the sweep is used before the HOME default. Optional direct claim metadata is also
accepted. An active claim with neither a resolved local path nor open lanes is
incomplete and refuses cleanup. Unreadable existing local records also refuse.

### Cancelled dependencies and new children

Cancelling an item removes its incoming `depends_on` edges from live gates and
goals in the same transaction, recording an `item_defined` event on each gate
with `dropped_cancelled_dependency`. Claimed/in-review gates advance epoch and
round so old evidence and verdicts cannot survive the dependency change.
Ordinary work dependencies and frozen done/cancelled gates keep their history.
An edge whose removal would leave live work without a path to a goal is kept
(cancelling a gate with live children stays legal); `kp check` reports it as
`CANCELLED-GATE-DEP` until that work is re-homed and the edge removed.

`define` / `create_items` report `refused` entries for new children whose `under`
gate still holds a cancelled dependency (for example, legacy data), while
creating the independent valid items. Each entry names the child, gate,
cancelled dependencies, and the `remove_dep` recovery. CLI text and JSON expose
these refusals and exit 2. Other validation failures still refuse atomically;
`apply_ops` always retains its atomic refusal behavior.

`kp check` prints `CANCELLED-GATE-DEP <gate> -> <dependency>` for gates and goals,
including frozen ones. JSON exposes `cancelled_gate_deps`; `--strict` exits 1.

### Claude Code review witnesses

Install the Rust `pg-seat-evidence` binary alongside the keeper. Attach evidence
kind `claude-seat` through the router evidence API with `transcript`, `review_file`
and full merged part `head` fields. `claude_projects_dir` optionally configures
its trusted transcript root (default: the keeper account's Claude projects dir).
The transcript must have the project/session/subagents/agent-ID.jsonl layout.
Every assistant message must name a Sonnet model and the matching session/agent.
A successful `Write` tool call must write the exact review file contents; end the
file with `HEAD: <full head>` followed by `VERDICT: ACCEPT`. Shell write commands
are not witnesses. Missing, changed or mismatched evidence gets a named refusal.
`close-merged` rechecks current-epoch transcript evidence, including inside its
transaction, and counts it with spawner seats. Existing writer-family, high-tier,
sensitive-card and lane-specific witness requirements continue to apply.
Lanes citing D-4504 retain their witness rule; a GO row must explicitly say `Witness: claude-seat` to use this kind.

**Priority levels and aging (KEEPER-PRIORITY-LEVELS-AND-AGING).** 10 = in flight or stops all
work; 9 = operator order of the last 2 days, a lesson that happened twice, or unblocks 2+ lanes;
8 = a lesson that happened once or unblocks one lane; 7 = planned work. An `LE-` item created
without a `priority` starts at 8; a new `INCIDENT LOG` line added to an `LE-` item's description
or note raises it one level (10 at most). The reaper drops an open, unclaimed item one level when
it stood at 10 for 3 days or at 9 for 7 days since its last `priority_set`/`item_created` event
and was not claimed since; `priority KEY <same value>` re-confirms and restarts the clock. Each
change is a `priority_set` event with `was` and `reason`. `digest` carries `priority_levels` (open
items per level) and `level10_warning` when level 10 holds more open items than lanes closed in
the last 7 days.
