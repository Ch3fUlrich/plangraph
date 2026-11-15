# HOST-IDLE-CAPACITY-ALARM

First slice: a reusable Rust host idle-capacity check and `host-idle-capacity`
fixture/snapshot CLI emit `IDLE <host> slots=<n> ready=<m>`. The flow watchdog
accepts the same snapshot and queues its existing routing alarm immediately.
Writer caps and reserved RAM reuse slot admission; disk uses the existing floor.

Open: live host/keeper collectors and wiring into resume-digest, orchestrator
heartbeat and the scheduler (these entry points are absent from this checkout).
No live collection or activation is claimed by this slice.
