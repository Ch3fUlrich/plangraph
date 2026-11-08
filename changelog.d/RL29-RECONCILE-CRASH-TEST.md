Crash recovery now records each launched attempt's process identity, never borrows
identity for explicit legacy attempts, and retains pid-zero rows as unknown.
Legacy unknown identities settle with reason unknown_identity after their deadline
plus ten seconds, or on the second restart without a deadline, releasing slots.
The dedicated Linux subreaper test uses bounded waits, a capped shell, production
lock observation, retrying socket connections, and kill/reap/unlink per restart.
