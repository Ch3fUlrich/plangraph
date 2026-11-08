# Fleet snapshot wiring (slice 1)

The persisted keeper snapshot writer attaches `fleet` using
`plangraph.fleet_snapshot.load_fleet`. This is a read-only projection outside
the keeper auth path. Loader failures produce `fleet: null`; plan snapshot
generation and signing continue normally. Failures log a traceback and add
`fleet_error: "projection_failed"`, making a broken store API visible. Direct store snapshot reads remain
unchanged.

`build_fleet(registry, samples=None, runs=None)` is pure. Every registry host
appears once, sorted by id, with `host_id`, `class`, `caps`, `sample`, `samples`
and `running_agents`. The live loader reads the existing persisted `bus_hosts`
registry; it has no class/caps columns, so those fields are null. It reads the
existing spawner run records from `PLANGRAPH_SPAWN_STATE` (otherwise the existing
XDG state default), using the watchdog reader's side-effect-free mode. Only
exact `running` records count, deduplicated by canonical host/run id. Queued
and terminal runs do not count. Missing records directories, malformed records,
unknown hosts or conflicting statuses yield unknown counts, not a false zero.
An explicitly empty records directory means known zero.

Follow-up **HOST-FLEET-SNAPSHOT-METRICS (slice 2)** owns sampler input (D-5051).
In this WIRING slice, `samples` defaults empty: CPU, RAM, GPU,
network RX/TX and disk fields are all null and each row has
`samples: "unavailable"`. Null means **unknown**, never zero. Slice 2 passes the API reader's host-to-metrics mapping
to this same loader parameter. Injected finite nonnegative values are copied;
invalid values become null, measured zero stays zero, percentages above 100
are unknown. Samples for unregistered hosts are ignored. No sampler, SSH
fan-out, process probe, ingest endpoint or background collection is added.

The existing `src/plangraph/web/page_hosts.js` reads `snapshot.fleet` and
renders every registry host once, with unknown metrics displayed as `unknown`.
Old snapshots without `fleet` retain the unavailable message. The SQLite-to-
snapshot-to-page integration test exercises this path. Registry aliases shared
by different hosts (including canonical ids) are refused, never last-wins.
