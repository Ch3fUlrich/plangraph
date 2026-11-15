# Host-local Tailscale link monitoring

Build and install `pg-host-link` from `rust/worker-core` on each build host.
The host session runs this foreground tick once a minute from its existing
host scheduler (one invocation at a time):

```
pg-host-link STATE_ROOT HOST_ID COORDINATOR_TAILSCALE_PEER
```

Use the same private `STATE_ROOT` as `PLANGRAPH_SPAWN_STATE` for the existing
stall watchdog. Supply the local host identity and the coordinator's Tailscale
IP or DNS name through host configuration; nothing is inferred from a session
name. The binary probes the peer with `tailscale ping`, rather than trusting
`tailscale status`'s online bit. The first failed probe starts an outage; only
a successful probe resets it. After strictly more than 300 seconds, one
`host-link-down` alert per episode is queued in `watchdog/outbox`, with host,
peer, failure start and observation time. Probe failures do not exit the tick;
configuration, probe-launch and state errors do, so the scheduler must report
nonzero exits. Each probe has a ten-second process bound and is reaped.

The existing [watchdog bridge](stall-watchdog.md) consumes these private JSON
events using `PLANGRAPH_WATCHDOG_DELIVERY`. It must deliver `kind=alert`,
`recipient=routing` to the keeper using a path independent of the failed peer
(for example the host session's still-connected Remote Control). A bridge that
depends on SSH to the coordinator cannot provide outage-time delivery. Events
survive recovery and are acknowledged by removal only after successful delivery.
Delivery is at least once; consumers deduplicate on `event_id`. State and event
files use atomic private writes. Corrupt state is an error, never a fresh outage.
Schedule gaps delay detection; this is not a claim of continuous observation.

Deployment acceptance on **both** build hosts remains required: confirm one
healthy tick, block only the coordinator peer, keep ticks running for more than
five minutes, and verify the keeper receives the host's alert through the
independent bridge. Unblock, tick successfully, then block again and verify a
new episode. Preserve the event IDs, timestamps and keeper evidence. Do not
block the Remote Control or keeper delivery path during this test.

This slice supplies the probe and durable alert producer plus the phase-batch
Remote Control fallback procedure. It does not install or enable host schedulers
or delivery bridges, certify either live host, or migrate ad hoc SSH waiters.
The native spawner's remote receipt polling already retains running records
when a query fails; external SSH waiters still need their exit-255 paths audited
and regression-tested without extending their deadlines or replaying launches.
