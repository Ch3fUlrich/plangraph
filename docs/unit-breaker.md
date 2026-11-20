# Watchdog breaker recovery (first slice)

`unit-breaker` is the Rust replacement for the external AutoOS watchdog breaker.
Build with `cargo build --manifest-path rust/Cargo.toml -p pg-worker-core --bin unit-breaker`.
The executable accepts:

```
unit-breaker --unit ctx-watchdog.timer [--threshold 5] -- COMMAND ARG...
unit-breaker status
unit-breaker reset ctx-watchdog.timer
```

It reads the existing `$XDG_STATE_HOME/autoos/breaker/<unit>.json` state
(default `$HOME/.local/state/autoos/breaker`), including legacy `tripped`,
`alerted` and RFC3339 `updated` fields. Malformed state fails closed with exit 2.
New failures are grouped by startup, I/O, memory, crash or other class. A class
change restarts the consecutive count. The command's exit code is returned;
130 and 143 leave state unchanged. Launch errors count as I/O failures.

At the threshold, the wrapper records the trip but keeps the timer enabled.
Calls during the next 15 minutes refuse to start the child. The first scheduled
call after that interval runs one half-open attempt, guarded by a per-unit lock.
Success clears the trip, counter, legacy marker and active needs-you file.
Failure renews the 15-minute cooldown regardless of failure class. There is no
internal retry, sleep, service-manager call or command timeout change.
Systemd should stop the entire service control group when cancelling a run.

A trip writes `needs-you/<unit>.json` alongside breaker state with owner
`routing`, and appends a `BREAKER <unit> tripped` line to `AUTOOS_ALERT_INBOX`
(or stderr when unset). Publication remains pending if either write fails.
Once both succeed, subsequent blocked calls do not duplicate the alert.
This is a durable local needs-you outbox, not a keeper plan item.
`status` prints only active trips; empty output with exit 0 means none tripped.
Routing should inspect status every heartbeat and reset a trip immediately if
it blocks every recycle, rather than waiting for a merge. Reset is local and
reversible; it does not enable a timer disabled by the old wrapper.

## Open integration work

This slice does not alter the deployed unit or pinned external router checkout.
Install the binary and wire the unit's ExecStart through it in a deployment
slice; enable any timer previously disabled by AutoOS during that migration.
The external ctx-watchdog/fleet_hosts probe must still be changed so an OSError
on one host produces a warning, exits 0 and permits ready local recycles, with
an end-to-end regression. That source is not versioned in this checkout.
The router heartbeat must consume `unit-breaker status` and the needs-you outbox
(and create a keeper needs-you item if required by its routing backend).
Until these integrations land, this change alone does not restore fleet recycling.
