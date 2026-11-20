# Session names and the admission window

Moved out of [SPAWNER.md](../SPAWNER.md).

## Session names (LE-SESSION-NAME-COUNTER-NOT-ALLOCATED)

Every session launched through the spawner takes `<role>-NNNN` from the host's one `session-seq`
counter (the flock'd file fleet-launch also draws from; `ROUTING_STATE_DIR` overrides its directory), for
every role. An explicit `<role>-NNNN` that is not the next host number is refused. See
[spawner sessions](../spawner-sessions.md).

## Admission window and queued runs (LE-SPAWNER-QUEUE-STUCK-AFTER-FLOOR-CLEARS)

A local spawn without room (memory floor, disk, or no admitting overflow host) is not left queued.
The spawning process records a `queued` run (`owner_pid`, `queued_until`), re-checks admission every
`PLANGRAPH_ADMISSION_POLL_S` seconds (default 15, at most 60) for `PLANGRAPH_ADMISSION_WAIT_S` seconds
(default 120), and then either starts the run (the waiting record is removed), places it on an
admitting overflow host, or ends it `refused` (exit code 2) with the admission reason. `spawner cancel`
of a queued run writes `cancelled` and ends the wait. `ps` and `status` mark a `queued` record whose
owner process is gone (or that has none, as records written before this change) with `stale: true` and
`stale_reason`; the stall watchdog sweep ends a record whose owner process died `refused` /
`queue_owner_gone` (an ownerless legacy record still ends by its `queue_timeout`).
A review reserves its role's registry ramp (`warmup.ramp_mb.review`), not the implement reservation, and a
refusal with no overflow host configured carries a `hint` naming `fleet_sync genconfig write`
(LE-REVIEW-RUNS-QUEUE-ON-CODING-VM-DEFAULT-CAPS).
