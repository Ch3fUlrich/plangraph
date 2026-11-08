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
and `dell_idle` describe placement; `host_drift` compares expected host code refs;
`closes_recent` and `closes_previous` count equal-length daily windows.
All fields are required; budget must be positive. Missing data alerts routing without writing state.
The seven condition groups produce eight rule names: placement and host drift are separate.

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
