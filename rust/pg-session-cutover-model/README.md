# Session cutover model

`Model::apply` is the transition table for one orchestrator role moving from today's waits to
the digest wait: waiter, pending wake, receipt, supervised recycle and per-message handling
records. Each event is one atomic transition; a refused event changes nothing. The model owns no
processes, files or clocks, and switches nothing live.

The invariant list, the transition table, the bounds of the exhaustive search and the map from
every review finding to its scenario test are in
[docs/session-cutover-model.md](../../docs/session-cutover-model.md). Sibling models:
[launch recovery](../pg-launch-recovery-model/README.md) and
[session admission](../pg-session-admission-model/README.md).

`tests/scenarios.rs` holds one named scenario per finding. `tests/exhaustive.rs` explores every
ordering of a stated alphabet to a stated depth and shows, for each `Defect` (a guard that a
finding named missing), that the invariant list catches the unsafe variant.
