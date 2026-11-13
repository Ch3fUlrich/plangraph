# Stall watchdog host contract

Both bootstrap scripts install the two-minute scheduler. Linux uses a user timer;
Windows registers a task for the installing user (Python must have plangraph on
its import path). No live scheduler is started by the test suite.

The watchdog reads `PLANGRAPH_SPAWN_STATE` (the spawner default otherwise).
Producers must attach `parent_session` and `item_key` to run records and touch
`session-activity/<session-id>` whenever the parent makes progress. Keeper claim
rows use `session_id`, falling back to `claimed_by`. Missing activity is unknown,
not idle. A running/queued record for the same item suppresses claim alerts.

Configure `PG_URL`, `PG_TOKEN_FILE`, and the spawn state in the scheduler's
environment. Linux additionally reads `~/.config/plangraph/watchdog.env`.

Events are private JSON files in `watchdog/outbox`. Set
`PLANGRAPH_WATCHDOG_DELIVERY` to a JSON argv array naming the host event bridge.
The bridge receives one JSON event on stdin, re-delivers `completion` events to
the parent, and routes `alert` events to the named recipient. Nonzero exit leaves
the event queued for retry; success acknowledges it. Without a configured bridge,
events remain durably queued: deployment must wire the bridge before claiming
end-to-end nudges. Delivery is at-least-once; consumers must tolerate duplicates.
Acknowledgement does not reset alert state: each condition/recipient has a private
persisted timestamp. `PLANGRAPH_WATCHDOG_REALERT_SECONDS` (default 1800) controls
re-alerting while a condition remains stalled. Invalid session identifiers are
skipped and reported in status rather than aborting other run checks.

`python -m plangraph.spawner.stall_watchdog status` emits the persisted `! stall` lines
for routerctl status to include. Routerctl lives outside this repository; its
host integration must invoke this command or read `watchdog/status.txt`.

The finished-child rule requires exit age and parent idle age over five minutes,
with no parent activity after completion. The claim rule requires over thirty
minutes idle and no live run. The next two-minute tick detects an eligible stall;
this means an exit can be nearly seven minutes old at detection, not a strict
five-minute wall-clock SLA from exit.
# Stuck OpenCode process trees (D-4220 / D-4221)

The existing two-minute watchdog tick also samples job trees; no additional timer
or installation is required. `watchdog/stuck-samples.json` stores process start
identities, proven ownership, host, summed CPU ticks and the beginning of the idle interval.
The host is `PLANGRAPH_HOST` or the local hostname and is included in findings/status;
samples from another host cannot establish ownership or continue an idle interval.
CPU changes, tree changes, PID reuse and non-loopback TCP connections (including
connecting/closing states) or connected UDP sockets reset that
interval. After 300 seconds a proven running job is cancelled through the normal
spawner cancellation path with reason `stuck_watch_idle_tree`. Log silence alone
does not authorize this cancellation: a buffered seat with an outbound connection
is not idle. The legacy log probe remains diagnostic in the production tick.

The motivating dell reviews on 11-06 (run-713f07d9, run-511c0ebc, run-576c669b,
run-9aeb4877) wrote 55–80 byte banners and then waited until the 1500-second review
limit. Process evidence detects this without relying on additional log bytes.

Terminal or missing-record job trees and proven orphan stdio trees use the existing
`reap.kill_proven` path. Ownership requires the job title and recorded start time
or a matching earlier proven same-host watchdog sample when the record is absent.
A present PID/start-mismatched record cannot inherit earlier proof.
Corrupt records, unreadable process/socket
evidence and initial idle samples do not authorize actions. TTY trees, service
trees and unproven processes are report-only. Unclaimed servers are reported
after five minutes; orphan stdio and protected interactive/service shapes are
reported immediately. Alerts use the existing routing outbox and re-alert limits.
Samples persist the last alert and action timestamps: the same condition on the
same process identity is emitted and acted on at most once per
`PLANGRAPH_WATCHDOG_REALERT_SECONDS` (default 1800). A changed condition or process
identity is eligible immediately. Unreadable individual PIDs are skipped; unreadable
or racing file descriptors mark the tree's socket evidence unavailable, report it
without action and reset its idle interval. Failure
to enumerate the process table preserves the previous samples without acting.
Dry-run reports without writing samples or cancelling/reaping anything. Platforms
without Linux process/socket evidence report that limitation in status only,
without routing alerts or signalling.
Check failures are isolated from completion and claim processing.

`read_runs` only reads snapshots; `sweep_runs` owns queued-run expiry and corrupt-record diagnostics during a watchdog tick.
