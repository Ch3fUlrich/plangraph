# Feature live probes (D-5050)

An item of kind `feature` carries `live_probe`: an absolute executable argv,
exact expected stdout (including newline), and optional `timeout_secs` (default
30, maximum 300). A keeper query is expressed as a read-only command returning a
small deterministic witness. Exit zero alone is insufficient. Commands are
trusted operator-reviewed configuration, not executable input from workers.

Minimal exported item envelope (the keeper schema/export hook is not installed):

```json
[
  {"key":"plangraph:OVERFLOW", "kind":"feature", "live_probe":{
    "command":["/usr/local/bin/pg-witness","overflow-to-dell","--since-seconds","86400"],
    "expected":"overflow-to-dell: witnessed\n"}},
  {"key":"plangraph:SPAWNER", "kind":"feature", "live_probe":{
    "command":["/usr/local/bin/pg-witness","spawner-served-model","--since-seconds","86400"],
    "expected":"spawner-served-model: witnessed\n"}}
]
```

These are example query-adapter contracts, not bundled executables or claims of
live success. The first must find a real overflow placement on the dell target
within 24h. The second must join a completed spawner run with its served-model
witness, verifying requested and actually served model identity within 24h;
configuration or a model-list response alone is not evidence. Return nonzero or
different stdout if no matching record exists. Use fixture adapters in tests;
never contact production while testing.

Build `cargo build --release -p pg-worker-core --bin pg-live-probe` under `rust/`.
Install the binary as `/usr/local/bin/pg-live-probe`, provision a locked-down
`pg-live-probe` account and read-only query adapters, and export reviewed item
envelopes to `/etc/plangraph/live-probes.json`. Install the three units from
`deploy/systemd/` and enable `pg-live-probe.timer` after deployment authorization.
No units are activated by this change. The runner appends and syncs JSONL results;
failures exit nonzero, trigger the alert unit and log at daemon.alert. Operations
must route that syslog priority to its monitored alert sink. Output is bounded
on read; systemd caps output-file size and overall runtime.

## Door integration required (not implemented here)

The engine and item schema are AUTH_DOOR_PATHS. The non-door Rust `close_gate`
requires the latest trusted result for the item to pass, match its current spec,
and be no older than 24h (future results fail). A newer failure invalidates an
older pass. Non-feature kinds retain their existing behavior. `pg-live-probe gate
ITEMS.json JOURNAL.jsonl` is the fail-closed adapter (0 pass, 1 refusal); the keeper
must generate a single-item envelope from its authoritative snapshot and read
only the runner-owned journal. Worker-provided result objects are not evidence.

Exact one-line hook required before **every** transition to `done`, inside its
write transaction, after reloading the item:

```python
require_live_probe(item, now)  # trusted adapter invokes pg-live-probe gate; nonzero -> Refused("live_probe", ...)
```

`require_live_probe` is an integration adapter still to be added by an authorized
door editor; it must not shell-interpolate item fields and must fail closed on
missing files, invalid JSON, timeout or nonzero exit. The hook is required on
normal verdict and decision/override close paths, not just submit. Authoritative
item storage/export and evidence attachment also require that authorized change.
Until those hooks land this is a runner + gate primitive, **not enforced keeper
closure**. Journal write access and spec changes require operator authorization;
deploy read-only mounts for the keeper and never let workers append results.
