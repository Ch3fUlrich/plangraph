# Code dispatcher (integration pending)

`pg-code-dispatch` is a timer-driven, model-free policy executable. It does not
choose a model or leg. The trusted bridge must route an opencode implementation
writer using the existing route utility without model or leg overrides.
The systemd templates are not installed or enabled by this change.

Set `PG_CODE_DISPATCH_BRIDGE` to an absolute executable path in the instance
environment file. Each invocation receives a verb argument and one JSON object
on stdin, and returns one JSON object on stdout. Credentials remain inside the
bridge; the dispatcher neither reads nor logs them.

Required bridge contract (not implemented in this change):

* `begin`: acquire a globally fenced dispatcher lease, return `items` and
  `routes`. Serialize overlapping timers and maintain the fence across calls.
  Items contain key, state, explicit code_only, done, files,
  operator_dependency, owner, and card. Resolve operator dependencies
  transitively. Routes contain host, harness, cap, active, usage_percent;
  aggregate active run records across all writers, not just this principal.
* `claim`: atomically compare ready state and claim under `code-dispatcher`;
  return a nonempty lease identifying the exact claim epoch.
* `spawn`: atomically recheck/reserve host capacity, use the route utility,
  require opencode, and persist a run record and owner completion instruction.
  Return run_id and harness. A nonzero exit must guarantee no run exists;
  ambiguous outcomes must return success with the persisted run identity.
* `release`: release only the supplied claim epoch and only if no run exists.
* `handoff`: durably register the run with the owner's GO-REQ completion flow,
  which must deliver the resulting PR for review (never auto-merge).
* `end`: release the fenced tick lease. Recovery must reconcile persisted runs
  and incomplete handoffs after a crash before admitting additional work.

Bridge failures stop or refuse dispatch; there is no direct keeper-write fallback.
Opencode routes are not throttled by Claude usage. Non-opencode routes are refused.
The bridge is a deployment prerequisite: this is not yet an end-to-end keeper
adapter. Live dispatch, utilization observation, PR merge, and CI verification
remain acceptance work and must not be inferred from local policy tests.
