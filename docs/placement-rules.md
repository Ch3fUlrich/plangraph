# Placement rule loader (R8B A)

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
row-preserving rollback belong to C3. See KEEPER's migration-gates section for GO.

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
floor. Worker cap defaults to 6, with semantic dispatch_vm=4 and prox=5 seeds.
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

The 400-line budget counts production lines only; sibling tests do not count
against that budget, and every file independently remains at most 400 lines.
Cargo builds the executable for the integration tests; no separate pre-build
or nested Cargo invocation is needed. Run `cargo test -p pg-worker-core
placement_rule`: the integration tests execute and reap Cargo's binary with
a cleared child environment.
