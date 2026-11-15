Added the first self-contained Rust breaker slice: legacy trip-state reads,
15-minute half-open recovery, success reset, per-unit locking, durable routing
needs-you outbox, alert lines, and an active-trip-only status command.
The timer stays enabled so scheduled probes can heal. Deployment wiring,
remote probe isolation/local recycle coverage, keeper needs-you ingestion and
router heartbeat integration remain open; see docs/unit-breaker.md.
