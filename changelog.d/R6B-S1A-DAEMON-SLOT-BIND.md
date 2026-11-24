The pg-spawnd supervisor can bind each run to a durable slot of the shared slot table before
its launcher call (`SupervisorCfg::slot_bind`, on in the daemon when reconciliation is `on`):
a waiting run holds a daemon-owned ticket, the grant turns it into a bound reservation that
must exist before the launcher is called, and a refused bind ends the run without a launcher
call. On restart, a pre-exec reservation or ticket left by a dead daemon is freed only on
proof (dead owner, a run record with neither a process nor the `launch_started` marker the monitor writes before the launcher call, no cgroup scope of the run); live, started
and unverifiable rows stay held.
Tickets and reservations carry the run's own role; the daemon refuses to start when the bind cannot be set up.
The queued run record is written before the ticket and the ticket before the monitor starts.
