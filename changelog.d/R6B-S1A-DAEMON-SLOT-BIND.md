The pg-spawnd supervisor can bind each run to a durable slot of the shared slot table before
its launcher call (`SupervisorCfg::slot_bind`, on in the daemon when reconciliation is `on`):
a waiting run holds a daemon-owned ticket, the grant turns it into a bound reservation that
must exist before the launcher is called, and a refused bind ends the run without a launcher
call. On restart, a pre-exec reservation or ticket left by a dead daemon is freed only on
proof (dead owner, a run record without a process, no cgroup scope of the run); live, started
and unverifiable rows stay held.
