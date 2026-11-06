# Host classes

## Migration

The version-1 registry remains external: no live hosts.json is committed or
provisioned here. Existing registries need no migration: omitted `class` defaults
to `worker`. Explicit values are `worker` and `restricted`; unknown values fail
parsing. Examples specify the label explicitly, but the schema keeps it optional.

`restricted` limits max_runs/max_writers/max_seats/max_sessions to 4/2/2/5
and requires mem_floor_mb >= 6144. Zero writer, seat and session subcaps remain
valid; max_runs must still be positive. Worker numeric validation is unchanged.
Policy follows the label, never a host id. The R8B L2 worker-cap seed of 5 is a
different counter and cannot widen max_runs.

Class is not an admission counter: existing slot admission uses parsed caps,
queues at full caps or below the memory floor, and retains its existing fairness.

HOST-AGENT owns collection, service and platform support; HOST-SAMPLES-API owns
ingest and storage. B consumes their payload and authoritative run records for
metrics/count; C owns web rows. This slice adds no sampler, deployment, persistent
service or R8 routing. Merge A, then B, then C from refreshed origin/main, each
with green CI and independent ACCEPT. Only C plus neighbour integration completes
the keeper item.
