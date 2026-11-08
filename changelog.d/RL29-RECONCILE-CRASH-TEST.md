Crash recovery now records each launched attempt's process identity, never borrows
identity for explicit legacy attempts, and retains pid-zero rows as unknown.
Legacy unknown identities settle with reason unknown_identity after their deadline
plus the named ten-second grace, or started/record mtime plus that grace without
a deadline. Missing attempts synthesize attempt one. Slot reclaim precedes the
terminal save, so failures remain retryable. Recorded pgids are probed even for
pid-zero legacy identities. Accepted risk: with no pid/group/pgid authority,
time-bounded settlement can release capacity while an unidentified child lives.
The dedicated Linux subreaper test uses bounded waits, a capped shell, production
lock observation, retrying socket connections, and kill/reap/unlink per restart.
