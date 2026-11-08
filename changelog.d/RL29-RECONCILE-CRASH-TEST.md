Added Linux end-to-end coverage for daemon SIGKILL after a real shell child exits.
Verifies orphan outcome/event uniqueness, reconcile-before-reclaim, and restart idempotence.
Fix round 1: retries never borrow run-level process identities or start
timestamps; unknown attempt duration is zero. Legacy records without attempts
and sole first attempts retain their unambiguous identity fallback. Inventory tables cover null identities, PID
zero, and PID reuse via start-time mismatch. Crash coverage observes contention
on the shared production lock path and socket readiness after reconciliation,
requires live restarted daemons, and caps the shell loop for panic cleanup.
The shared lock-path helper requires a one-line production caller change in
reconcile_serve.rs; the subreaper test remains alone in its dedicated binary.
