Crash recovery now records each launched attempt's process identity, never borrows
identity for explicit legacy attempts, and retains pid-zero rows as unknown.
Legacy unknown identities settle with reason unknown_identity after their deadline
plus the named ten-second grace, or started/record mtime plus that grace without
a deadline. Missing attempts synthesize attempt one. Slot reclaim precedes the
terminal save, so failures remain retryable. Recorded pgids are probed even for
pid-zero legacy identities. Accepted risk: with no pid/group/pgid authority,
time-bounded settlement can release capacity while an unidentified child lives.
Pid-zero pgid-only rows compare recorded start times against the group leader
to reject pgid reuse. Without a start time, even a probed-live pgid settles after
the same persisted ten-second unknown-identity grace (deadline/started/mtime);
this deliberately accepts the same capacity-release risk for unverifiable groups.
The dedicated Linux subreaper test uses bounded waits, a capped shell, production
lock observation, retrying socket connections, and kill/reap/unlink per restart.
Linux grace eligibility now requires a matching recorded start time on a live
or zombie (Z/X) leader to retain capacity indefinitely; a confinement scope name alone
is not identity proof. This also bounds live-pid/no-start, dead-pid/live-pgid,
mismatched-start, and populated-scope legacy rows. The 54-row identity table
(3 pid x 3 pgid x 2 group x 3 start-time states) proves 46 rows orphan and reclaim
their slot by the first reconciliation after the persisted ten-second grace;
only eight rows with matching live leaders remain held. Existing deadline,
started, and mtime anchors and the accepted unidentified-child risk are unchanged.

Round 7 extends the real Linux table to 162 rows (live/Z/gone): 146 settle and
16 matching live/Z identities remain held. A portable 216-row decision table
also covers X (not reliably constructible as a real process): 192 need grace,
24 matching live/Z/X identities do not. That same table is compiled on non-Linux;
the platform probe additionally checks that all 216 identities need grace there,
because this implementation has no portable leader start-time observation.
An empty Windows job can still prove gone immediately; a populated or unknown
group, including pid zero with no start time, uses the persisted grace bound.
Linux zombie leaders retain capacity while their start time matches, since
their process-group members may remain alive. Without a start time the existing
bounded unidentified-child risk applies. A direct orphan_gone regression proves
that a running row with no identity receives both the unexpired and expired
grace decisions; the optional current-row lookup is not the predicate argument.
Round 8 supersedes the indefinite Z/X hold above: Linux scans /proc/*/stat
for non-Z/X members of the recorded pgid. A memberless zombie settles immediately;
a zombie with live members is held only until UNKNOWN_IDENTITY_GRACE_MS (10s)
after the persisted deadline/started/mtime base. Non-Linux Unix uses kill(-pgid, 0)
with the platform gap that it cannot distinguish zombies; grace still bounds it.
The real 216-row live/Z/Z-member/gone table retains only eight matching live
leaders and settles 208 expired rows; fresh zombie rows test both member cases.
The portable 216-row table marks 208 rows grace-eligible and eight verified.
A direct current=None regression tests unexpired and expired missing-identity
grace. Round 9 corrects absent-inventory settlement: saved confinement is always
probed, and a verified live leader/group cannot settle until gone. Missing records
and terminal records with unverifiable identities retain the ten-second hold,
even for an empty confinement. Missing records use the saved deadline or run
directory mtime when no record anchor remains. The accepted post-grace legacy
capacity-release risk remains unchanged. Fresh/expired Z-member and immediate
memberless-Z settlement remain covered by every_running_identity_is_bounded_or_verified.
Known gap: probe after unknown-identity grace and zombie-branch bound tracked in FU-RL29-SETTLE-PROBE.
