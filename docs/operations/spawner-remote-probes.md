# Remote run probes

`stall_watchdog tick` samples recorded runs without their launcher, reports
silent runs, orphans and implement exits without completed work, and cancels
only proven silence of twice the card's `stall_min` (default 12 minutes).
Unreadable evidence is unknown, never permission to cancel.
`python3 -m plangraph.spawner.remote_probe --host NAME --notify-cmd CMD`
pulls durable findings over SSH. Repeat `--host` or set `PLANGRAPH_PROBE_HOSTS`;
the notification command can use `PLANGRAPH_PROBE_NOTIFY_CMD`.
Installation creates `plangraph-remote-probe.timer`, disabled by default;
configure its environment and explicitly enable it on the polling host.
Set `PLANGRAPH_PROBE_ROOT` to the remote checkout root; SSH uses its `src` tree.
Completion IDs are stable per run and rule, independent of changing ages.
Notification reservations persist before CMD, preventing replay across restarts.
A crash before spawning CMD can lose delivery; exactly-once external effects require
an idempotent transactional receiver. The event ID supports reconciliation.
Resetting corrupt probe state can replay previously reserved findings.
Unknown evidence is delivered, never considered silent and never cancelled.
