# Placement rule loader (R8B A)

## Python spawn overflow (HOST-RUN-CAPS slice 2)

Default CLI/MCP admission and SSH target selection use the registry role RAM
reservation above `caps.mem_floor_mb`; live run counts do not limit admission.
Healthy, fresh targets are ranked by available RAM above their floor, then name.
The overflow routing file supplies `hosts`, the `public_repos` allowlist and
the orchestration thresholds below; legacy local limit overrides are ignored. A repository absent from the allowlist
queues without copying. Exhausted targets produce per-host safe refusal reasons.
Without an overflow config, local admission still applies: admitted local runs
have no overflow annotation; below-floor queued records carry matching `reason`
and `overflow_reason` with the memory refusal and absent/unknown allowlist reason.
Missing or unreadable receipt IDs produce `acknowledged_unknown=true`,
`status=unknown` records on the selected host, never a refusal or retry.
See docs/SPAWNER.md for registry defaults and receipt boundaries. This Python
integration does not change the Rust placement-rule rollout gates below.

### Remote-first orchestration (D-5188, slice 1)

An optional per-host `orchestration_only: true` in `hosts.json` (or the local
entry in `overflow-hosts.json`) changes unnamed implement/review/research spawns
to remote-first; absence defaults to false and preserves local-first overflow.
Eligible remotes still require fresh healthy readiness, SSH, registry RAM caps
and an allowlisted public repository. Rank by `MemAvailable - mem_floor_mb`,
breaking ties in overflow hosts configuration order, never by run counts.
Only when no remote admits may a healthy local host run the task. Research uses
the existing orchestrate model policy; its RAM reservation keeps the research role.

The overflow config's `local_min_available_mb` defaults to **4096** and
`local_max_swap_used_pct` to **90**. Local fallback refuses below the minimum
MemAvailable (`local_min_available_mb`) or above the swap-used percentage
(`local_max_swap_used_pct`); equality is healthy, and no configured swap is 0%.
Registry role reservation/floor admission still applies. Refusals are explicit
`status=refused`, exit 2, never queued; private repositories are never copied.
Durable spawn/refusal records include `placement: {host, reason, free_mb}`.
Reasons are `remote_most_free_ram`, `local_fallback_no_remote`, either threshold
name, or a RAM refusal. A local-only benchmark database uses the distinct reason
`local_benchmark_database`; it does not imply that healthy remotes are absent.
Missing, malformed or unreadable swap readings refuse local fallback as
`local_swap_unavailable`, without preventing an eligible remote launch. `free_mb`
is the selected host's sampled MemAvailable, not its headroom. Remote launch
failures exclude that target before trying the next target or local fallback.
Planning is separate from admission: `plan_spawn` accepts an optional placement
snapshot and always serializes the field, including error plans. Without a
snapshot, plans and route previews explicitly report
`{host: null, reason: "placement_not_evaluated", free_mb: null}`; `route()` never
performs readiness/health probing or reads local memory/swap for placement.

## Operator view: local Rust rule source

`placement_rule_store::RuleSource` accepts a configured env-file path (or None),
private cache path, opaque host ID and trusted registry host class. `load` reads
the real process environment afresh; `load_with_env` injects a map for tests.
Neither local layer is persisted. `LoadedRules` exposes the effective report,
keeper revision and `keeper`/`lkg` source; report sources identify each winner.

| Priority (highest first) | Input |
| --- | --- |
| 1 | Current process `PG_RULE_` environment |
| 2 | Current configured env file |
| 3 | Keeper override for the exact host ID |
| 4 | Keeper fleet override |
| 5 | Schema default, including trusted class seed |

| Key | Default / seeds | Inclusive range |
| --- | --- | --- |
| `PG_RULE_HOST_MEMORY_FLOOR_MIB` | 6144; alarm metadata 3000 | 0–1048576 |
| `PG_RULE_HOST_WORKER_CAP_COUNT` | 6; trusted host-class seeds a host → 4, a host → 5 | 0–1024 |

Values are unsigned decimal integers; schema allow-lists and ranges remain
authoritative. Unknown `PG_RULE_` keys are refused with redacted diagnostics;
unrelated environment keys are ignored. Invalid overrides fall to the next layer.
For a local override put `PG_RULE_HOST_WORKER_CAP_COUNT=7` in the configured file
or process environment. This adapter parses each file line independently: blanks
and comments are ignored, malformed or non-UTF-8 lines are reported/skipped, and the first
duplicate wins with a diagnostic. It does not run shell syntax or interpolate.
Missing files are empty; unreadable files are reported and treated as empty.
The standalone strict `parse_env_file` API below retains whole-file rejection.

For a durable per-host override, use the authenticated keeper edit surface with
`scope={"Host":"worker-a"}`, the schema key, string value and current
`expected_revision`. Fleet uses `scope="Fleet"`; null value removes an override.
Edits require authorization and atomically append audit history. Rollback appends
a new revision, never rewrites history. Local overrides still take precedence.
Only keeper Unavailable enables validated LKG; other keeper errors fail closed.
Current process/file changes still apply during an outage with a valid LKG. No valid LKG means
Unavailable, not silent defaults. This adapter does not wire admission or deploy.

## C5 keeper specs and report projection (rulings 1–3)

`placement_rules_report` and `placement_rule_specs` use C4's persistent read
identity policy. The report materializes one coherent snapshot under one write
transaction, then calls C1 outside that transaction. The internal
`_placement_rules_projection` hook runs inside its caller's transaction for C6's
snapshot.fleet integration. Specs and every resolved value come from Rust.

The shape is `{snapshot,specs,fleet,classes,hosts,context,source,revision}` with
`context="keeper_only"` and `source="keeper"`. Fleet has no class or host layer;
Class projections have no host layer. Each stored host ID uses an
empty class: its worker seed is fleet default 6 unless overridden, even if its
ID happens to equal a class label. Class labels are not machines. C1 currently
requires a nonblank transport host ID, so fleet/class calls use a fixed sentinel
with an empty host map; it can never select a stored host override.
Memory floor is 6144 MiB, alarm metadata 3000 MiB; worker seeds are fleet 6,
dispatch VM 4 (D-081), live L2 Proxmox class 5 (D-1485).

Process and env-file layers are always empty. This is not the executing host's
effective admission report and never guesses registry class from an ID. C7 owns
current caller environment and cached source; C6 transports this keeper-only
shape; C8/C9 display context. A tests cover invalid higher-layer fallback with
diagnostics; corrupt stored keeper overrides instead fail closed as rules_invalid.
B covers LKG; C9 ties done acceptance together.

C5 limits raw serialized snapshots to 1 MiB and 128 distinct hosts. C4 edits
check prospective bounds before audit/commit, rolling back revision and values
on refusal, reserving space for C1's bounded request envelope as well.
Host IDs must be nonblank, control-free, at most 64 UTF-8 bytes.
They are opaque keys, never filesystem paths, refs or shell input. All keeper
paths here remain AUTH_DOOR_PATHS doors. D-4337/#539 fixture fixes reuse the
up-to-date helper and use a bridge-local test clock without global mutation.
No live host inputs, deployment, migration or admission cutover is implied.

## C4 keeper transaction adapter (ruling 5)

Persistent authenticated principals read coherent RuleSnapshot values; only a
persistent router edits or reads the rule ledger. Run/oversight tokens and the
synthetic system identity cannot use these public methods. Project membership
never grants fleet administration. C6 owns endpoints and internal system reads.
Each edit rechecks identity inside the existing keeper write lock, validates the
exact RuleEdit through C1, and commits revision CAS, value and immutable RuleAudit
together. Rollback verifies scope/key/previous and appends a new revision, not an
item event. The existing request audit is separate and may record refusals.
Commit ambiguity remains fatal; edits are never automatically repeated.

The server-only absolute PG_PLACEMENT_RULES_BIN defaults to
`/usr/local/bin/pg-placement-rules`. C4 uses fixed argv, empty child environment,
bounded stdin/stdout and a five-second deadline, reaping only its owned child.
Rust alone owns allow-list/value policy. Every touched keeper module, including
the bridge, remains an AUTH_DOOR_PATHS door. Helper installation, API restart and
schema application require operator GO; no live operation is performed by C4.
Ledger pagination is revision-ascending, with since >= 0 and limit 1..100.

## C2 existing-keeper SQLite storage (D5)

Schema v17 installs revision, override and immutable audit tables in the existing
keeper; no new store or database file is introduced. Missing singleton means
logical revision zero; C4's first edit transaction inserts it. Installation is
presence-based and DML-free, with no seed overrides: A owns defaults/seeds.
`edit` holds exact canonical RuleEdit JSON including `expected_revision` and
`rollback_of`; `previous` is nullable. Audit UPDATE/DELETE abort. Rollback is a
new forward audit event, never an item event or a fabricated item key. No secrets
or host metrics belong in these tables. C4 owns transaction/audit behavior;
C2 alone does not satisfy item done criteria or claim closure. PG parity and
row-preserving rollback belong to C3. See the migration-gates section below for GO.

### Migration-gates: placement rules C2 (D5 / GO)

Schema application is a GATED STEP: operator GO must name the reviewed SHA and scope, with a backup and offline rehearsal before application. Only the operator may approve keeper restart/SQLite upgrade; C3 owns the migrate-role PostgreSQL step and row-preserving rollback. Writers run only temporary fixture databases, never open live config/DB, print secret DSNs, or clean up outside fixture ownership. All touched keeper modules are auth-door paths, not a Hand-Edit exemption. Downgrade disables rules endpoints first and preserves the ledger: never drop audit rows. C4 owns transactions, including rollback as a new forward audit event; this is not the item events ledger. C2 installs storage only, not item closure.

`setup/placement-rules.schema.toml` is the sole metadata authority, embedded at
compile time. `schema()` fails closed with `rules_schema`; it never substitutes
defaults after a parse failure. The parser accepts only version 1, rule records,
single-line quoted strings (no escapes), unsigned decimal integers, string
arrays, and inline string-to-integer tables. Blank and full-line comment lines
are accepted; other TOML features are unsupported.

UI consumers serialize `RuleSpec` and `RuleReport`, not a second schema.
All override maps use derived `PG_RULE_<UPPER_SNAKE_NAME>_<UNIT>` keys.
Resolution tries process, env_file, keeper_host, keeper_fleet, then default.
Disallowed sources and invalid values are reported without supplied values,
then fall through. Unknown PG_RULE keys are refused and reported with a redacted
key marker; unrelated environment keys are ignored.

`parse_env_file` accepts trimmed KEY=VALUE and blank/comment lines, never shell
execution, export, expansion or interpolation. Duplicate keys fail the whole
file: callers report `env_file_invalid` and use an empty env-file layer.
Error Display codes are `rules_schema`, `rules_io`, and `rules_override`.

Memory floor defaults to 6144 MiB; 3000 MiB is alarm metadata, not an admission
floor. Worker cap defaults to 6; semantic class overrides and their worker seeds
are defined by the Rust policy and placement-rule schema.
Only a trusted host-registry adapter may supply the semantic class. These policy
defaults cannot widen explicit hosts `Caps`; admission integration is deferred.

## B transaction boundary and integration

`placement_rule_store::edit` validates A's allow-list, scope and values before
calling the adapter. Actor is authenticated keeper identity, not request JSON.
`RuleStore::commit` requires one durable transaction for revision CAS, override
mutation and immutable audit append. Errors expose stable codes, not values.
Rollback verifies the earlier event's scope/key/prior value and appends a new
revision; `None` removes an override, including rollback of an initial insert.
The hermetic test store demonstrates the contract, not production durability.

`load_cached` validates keeper snapshots through A before atomic cache replace.
Cache-write failure logs a redacted diagnostic and returns the valid keeper snapshot.
Only Unavailable permits validated version-1 LKG fallback, without offline expiry.
The private adapter-owned cache is bounded to 1 MiB, excludes process/env-file
layers, rejects symlinks/nonregular files and uses a sibling lock/temp plus sync
and rename. The adapter must display revision and online/offline source and
resolve current process/env-file inputs afresh. No unauthenticated edit CLI is added.

Dependencies before item closure: authenticated keeper endpoints, durable
transaction adapter and event mapping, UI transport using A specs/report,
snapshot.fleet enrichment, migration/GO, and R8 routing/cutover consumption.
No Python scheduling logic or second database belongs to this boundary.
A tests cover every precedence level, invalid-value fallback/reporting and seeds;
B tests cover precommit refusal, atomic audit, forward rollback and outage LKG.
Item closure still requires green CI and ACCEPT outside every writer family.
Role rollout remains off/shadow/on and gated on R7. HOST-AGENT and
HOST-SAMPLES-API retain independent services/stores and all sampler, ingest,
staleness and retention ownership; they supply no rule inputs.

## C1 pure Rust bridge

`placement_rule_bridge::handle` accepts strict JSON requests: `{op:"specs"}`,
`{op:"validate",edit:RuleEdit}`, or `{op:"report",snapshot:RuleSnapshot,
host_class:String,host_id:String,process:Map,env_file:Map}`. Results are respectively
`{specs}`, `{}`, and `{specs,report}`. Scope retains Rust's wire format: `"Fleet"`
or `{"Host":"id"}`. Validation delegates to `validate_override`; reports validate
every stored override before using the selected host and fleet layers with A's
resolver. Unknown fields, operations and stored keys are refused. Host IDs are
opaque data keys, nonblank, without control characters, at most 64 UTF-8 bytes;
revisions must fit a nonnegative signed 64-bit database integer.

`pg-placement-rules` takes no arguments and reads one JSON document (at most
1 MiB) from stdin, emitting one `{ok:result}` document (exit 0) or
`{error:stable StoreError code}` (exit 2), without echoing input. It does not read
environment values, authenticate, edit, cache, access the network or run commands.
An actor string does not authenticate anyone, including at the Rust edit boundary.
Report process/env-file maps and host class are trusted adapter inputs, never
keeper HTTP request-body fields. Later slices own authenticated transactions
and HTTP RuleStore integration; this bridge alone does not close the item.

Rule values retain RuleReport's u64 JSON representation. Browser consumers must
reject integers outside their safe integer range rather than silently round.
No auth-door paths, admission policies or host-registry defaults change here.

The 800-line budget counts production lines only; sibling tests do not count
against that budget, and every file independently remains at most 800 lines.
Cargo builds the executable for the integration tests; no separate pre-build
or nested Cargo invocation is needed. Run `cargo test -p pg-worker-core
placement_rule`: the integration tests execute and reap Cargo's binary with
a cleared child environment.

## C3 PostgreSQL migration-gates (D5 / S7 GO)

Migration 11 adds rules to the same keeper database, schema and connection,
not a Rust-owned database. C2 columns and constraints use BIGINT (signed 64-bit
revisions), C-collated TEXT, canonical JSON TEXT, no sequences and no seeds.
Audit UPDATE/DELETE/TRUNCATE are rejected; snapshot copy includes all rule tables.

Deployment is applied through the keeper's migrate role under the S7 GO.
Live steps need a GO per sha and scope (D-825): disposable local PG schema
rehearsal; operator backup verification; migrate-role application; runtime-grant
verification; approved API restart; rollback rehearsal and operator-only
retirement. The migrate deployment procedure grants runtime only existing table
DML permissions, never DDL rights. Writers do not apply live steps, issue
credentials, restart services or change hosts. Never disclose connection strings or secrets.
Every touched keeper path is an AUTH_DOOR_PATHS door, without Hand-Edit/gate
bypass. See docs/KEEPER.md for keeper operations; this scoped section holds the
new gates without shrinking or rewriting that existing oversized document.

Keep application writers stopped throughout rollback. `rollback_pg` accepts a
raw psycopg migrate-role connection with approved keeper search_path, not the
guarded PgConnection. Under the migration advisory lock and exclusive locks on
the three fixed tables it renames tables and primary-key indexes to
`*_retired_v11`, preserving all audit and current values and removing only
migration 11's record. Existing retirement names refuse atomically; no drop,
truncate or CASCADE. Reapplication refuses a retired ledger: require an explicit
restore plan, never silently introduce revision zero alongside retained history.
SQLite rollback is service/config rollback retaining tables. C4 owns transaction
behavior and forward audit rollback; C6 owns routes. Mandatory ephemeral PG CI
must pass before merge; C3 storage alone does not close the item.
# Host readiness consumer contract

`python3 -m plangraph.spawner.host_ready` checks each overflow host, including
its configured spawner source and matching `ps-hosts.json` source. Every overflow
host requires a matching ps entry; an absent entry (including a missing ps config)
fails with `src_config missing_ps_host`, never readiness success. Source entries
are deduplicated before validation, so repeated invalid entries report once.
Unreadable or malformed ps configuration reports only `src_config unreadable`,
not a contradictory missing-host reason. Source paths must be nonempty strings,
exist and import `plangraph` with that path as
`PYTHONPATH`; non-string paths fail with `src_config`, not a traceback. The ps
entry's `target` and overflow `ssh` must be the same string: different aliases for
the same machine are still rejected. The `src_config` mismatch line names both
values and says they must be the same string. Unsupported source transports fail
closed. A session probe must be configured.

After fetching origin/main, `behind_main` reports the number of commits in
`HEAD..origin/main`; unreadable or unreachable means JSON `null`, never zero.
SHA equality is checked separately, so divergent or ahead checkouts also fail.
Memory, disk, doctor, keeper reach, session and capacity remain independent checks.

For routerctl to grep (no private-repository change required), every failure is
one stable line: `HOST-NOT-READY <host> <check> <reason>`. These lines go to stdout
normally and stderr with `--json`, preserving machine-readable JSON stdout.
Source checks for routerctl's grep set are `src_missing` (absent path),
`src_import` (import failed), `src_config` (invalid, unsupported or unreadable
configuration, or unknown route/path), and `src` (probe failure or unexpected
response). The spawner source is probed even if ps-hosts configuration is invalid.
Pre-probe configuration errors use `config`, not `unreachable`.
Ordinary per-host configuration/probe exceptions and state-write failures also
produce failure lines without aborting other hosts' reports; exception text is
not exposed. Resource exhaustion (`MemoryError`, `RecursionError`) and
process-control exceptions propagate instead of being relabelled as config errors.
Lag uses `behind_main` and reason
`N behind`; unreadable lag emits a `behind_main` failure line. Unreachable hosts
report `unreachable`, leaving other values unknown.
All-green hosts emit no such line. Reasons never include raw transport output.

Live acceptance remains a deployment obligation: demonstrate both hosts green,
merge/CI success and a non-writer-family review seat. Hermetic tests do not claim
that evidence. Named defect: configured source checkout deleted while remote ps
continued using its stale configured path; the source probe now detects this.
