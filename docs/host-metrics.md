# Host metrics projection

`pg_worker_core::host_metrics` projects injected values, not a second sampler.
`HostSample` carries CPU/GPU percentages, available RAM, RX/TX bytes per second
and free disk bytes. Missing/unreadable metrics serialize as null; measured zero
survives. `normalize_sample` independently nulls nonfinite/out-of-range percentages.
`summarize` normalizes selected samples, resolves aliases to canonical host ids,
ignores unregistered sample hosts, and emits all registry hosts sorted by id.
Latest server `received_at_ms` wins; equal timestamps use the last input.
No local-clock staleness is inferred.

Counts use only authoritative records with `host`, valid `run_id`, and `status`.
Only exact `running` counts, once per canonical host/run pair, not per attempt.
Conflicting statuses or malformed records make fleet and affected host counts
unknown; unassignable/unknown record hosts make every host unknown. Missing
records mean unknown; an explicitly empty array means known zero. No process,
PID, CPU or session evidence is used. Extra record fields are not projected.

## Integration

HOST-AGENT owns the single Linux/Windows/WSL2 stream (WSL2 own node/cap), GPU
NVML/fallback, network-rate conversion, disk collection, privacy filtering,
60-second service, installation idempotence/drift and CPU budget.
HOST-SAMPLES-API owns authorized snapshots, server received_at/sequence gaps,
stale/down metadata, Postgres tests, range partitions, retention/rollups,
ingest-only tokens and owner-role migration. Its read adapter must map its
public payload into `HostSample` and attach `FleetSummary` as `snapshot.fleet`
before C can be live. That adapter is an explicit missing integration dependency,
not completed here. C owns display only. This slice adds no service, polling,
ingest route, migration, sampler or change to `host_probe::probe`; it does not
claim neighbour done clauses or the keeper item's web view are complete.

## Hosts page (C)

The accessible Hosts tab supports `#pg=hosts` and reads only `snapshot.fleet`.
It displays every supplied host sorted by `host_id`, including unsampled hosts;
fleet and row running-agent counts are supplied values, never reconstructed.
The wire shape is `{hosts: [{host_id, class, caps, sample, running_agents}],
running_agents}`; sample fields follow `HostSample` above. Caps show max_runs,
max_writers, max_seats, max_sessions and mem_floor_mb with their field labels.
Missing metrics/counts display `unknown`, measured zero remains `0`, and missing
fleet displays `unavailable` rather than a known empty fleet. Malformed rows
remain visible as unknown data. All supplied strings use DOM text, not HTML.
Hermetic Node/DOM tests use fake snapshots only. No fetch or sampler is added.
Live rows still require the neighbour adapter; closure also requires A/B/C,
green CI and outside-writer-family ACCEPT arranged by the merger.
