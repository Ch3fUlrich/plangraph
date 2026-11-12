# Flow watchdog (D-5050)

Build `cargo build --manifest-path rust/Cargo.toml -p pg-worker-core --bin
flow-watchdog --release`; install the binary in `~/.local/bin`. Copy the service
and timer from `deploy/watchdog` into the systemd user unit directory. Configure
`~/.config/plangraph/flow-watchdog.env`, then enable the user timer. Installation
and live activation are intentionally not performed by tests.

The checker consumes a required JSON snapshot, not an LLM. A host collector must
atomically refresh `~/.local/state/plangraph/flow-snapshot.json` before each tick:
`pace`, `budget` are weighted daily Claude pace and budget from
`tools/claude_usage.py pace`; `ci_failed`, `ci_total`, `infra_failed` count the
same CI window (infra failures are a subset of failures); `review_age_hours` is
the maximum age of an in_review item; `review_rounds` is the maximum per PR;
`critical_idle_hours` is the #690 critical-path idle measurement; `coding_vm_runs`
and `worker_idle` describe placement; `host_drift` compares expected host code refs;
`closes_recent` and `closes_previous` count equal-length daily windows.
The fields above are required; budget must be positive. Missing data queues a durable invalid-snapshot alert.
The seven condition groups produce 8 rule names: placement and host drift are separate.

Collectors may also supply `hosts` (an omitted list defaults to empty for compatibility).
Each entry requires `host`, `tmp_used_percent`, `root_used_percent`, `checkout_head`,
`origin_main_head`, `behind_commits`, and `age_hours`. `host-disk` alerts strictly
above 85% used on either `/tmp` or `/`. `host-code` alerts when the spawner checkout
is behind origin/main by more than 10 commits **or** 24 hours. The collector must
provide the ancestry-based behind count and age in hours since the first missing
origin/main commit, not the checkout commit's timestamp. Equal heads or a zero
behind count do not alert merely because the checkout is old. These two additional
conditions emit stable per-host identities with JSON-escaped identifiers; no Git or host access
is performed by the checker. Collectors must refresh origin/main before measuring.

Routing uses the existing `PLANGRAPH_WATCHDOG_DELIVERY` JSON argv bridge contract
(see stall-watchdog.md): one JSON alert on stdin with recipient `routing` and one
message line combining all firing rules. `PLANGRAPH_FLOW_PUSH` is an explicit
JSON argv push hook receiving the same event. No existing push implementation or
#690 implementation was found in this checkout; measurement reuse is an input
contract, not a claim of live integration. Both hooks must be wired by deployment.
Without a hook, events wait in a durable queue. Successful channels are
checkpointed independently. Identical active sets are suppressed until recovery
or a changed set; delivery is at-least-once across a crash during acknowledgement.
Systemd prevents concurrent ticks of this service; do not run manual concurrent
instances against its state directory. Routing advances despite pending push;
the queue retains its head and newest 255 events, reporting overflow on stderr. Collectors, freshness and host integration
are deployment prerequisites, not supplied by this checker.

## Optional turn-budget reader

Set `PLANGRAPH_TURN_BUDGET_STATE_DIR` to the fleet state root to opt into the
small read-only Rust status reader. Unset, the watchdog never reads session
counts. The reader scans `<state>/status/*.json`, skips corrupt/unrelated files,
and recomputes each rolling-hour count at the tick's time. See
[Turn-budget hook](turn-budget-hook.md) for the producer and schema.
Over-budget sessions each enqueue one exact `TURN-BUDGET <role> <session>
wake=<n>/<cap> hour=<n>/<cap>` message, without a `FLOW-WATCHDOG:` prefix;
both caps firing still yields one line. The existing active-set/queue machinery
suppresses identical lines until recovery or changed counts. No collector,
settings change, hook installation or live activation is performed by this lane.
