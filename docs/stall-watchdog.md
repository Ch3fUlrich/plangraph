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

`python -m plangraph.spawner.stall_watchdog status` emits the persisted `! stall` lines
for routerctl status to include. Routerctl lives outside this repository; its
host integration must invoke this command or read `watchdog/status.txt`.

The finished-child rule requires exit age and parent idle age over five minutes,
with no parent activity after completion. The claim rule requires over thirty
minutes idle and no live run. The next two-minute tick detects an eligible stall;
this means an exit can be nearly seven minutes old at detection, not a strict
five-minute wall-clock SLA from exit.
