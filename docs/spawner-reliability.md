# Spawner reliability

## RL12: Unix event supervisor (P-R6)

`watch::supervise_events` accepts the already confined, jail-wrapped `Command`.
It does not reconstruct argv or environment and has no harness caller until
RL14. This is a host supervision slice, not Temporal worker adoption.
Rollback is `git revert`.

Separate bounded stdout/stderr readers run concurrently with the stdin writer;
EOF closes the prompt pipe. Only stdout enters the event parser. Both streams
refresh the byte clock and contribute to the capped private output tail. A
bounded queue and bounded drain prevent a producer from starving supervision.
No pipe thread is joined: escaped pipe holders cannot block the return beyond
the two-second drain deadline. Detached workers retain only bounded chunks and
the supplied prompt until their pipe closes; no payload enters result metadata.

The supervisor drains pending signals before ticking the monitor every 10 ms.
The monitor supplies warning/confirm-window decisions. Cancellation wins over
wall timeout, then Blind byte stall and event decisions. Provider errors and
I/O cleanup use the same Stop path. Once selected, a reason is never replaced;
signals during teardown are calibration only (`late_signal_ms`). Tree hooks
are sampled at start and every 30 seconds; callers supply writer-only sampling.
CPU hooks provide signals, never a separate liveness bypass.

Aggregate channel health mirrors parser counter deltas without parsing payloads
again. Its first Blind reason is sticky; `blind_events = 1` is the single RL14
`CHANNEL_BLIND` handoff. Blind decisions become would-warn/would-kill, and only
Blind channels arm `Opts.stall` (None disables it). A parsing silent tool never
arms the byte deadline. RL14 supplies the configured 600-second default;
`Opts::default` deliberately does not change. Plain mode-off callers continue
using `stall`.

The owned backend captures a fresh pgid at spawn, guards every signal with a
zero-signal probe, and permanently latches ESRCH. EPERM never proves emptiness.
Root reaping does not lose ownership of surviving descendants. Stop probes at
signal points and its final proof deadline, not every tick: TERM once, grace,
KILL once, then a bounded proof interval (production: 2000 ms). Survivors is
terminal kill-incomplete, never permission to fall through. Natural root exits
also clean up surviving group members. Adopted identity validation belongs RL29.

Tests use synthetic parsers and local sh children, scaled integer-second event
limits, and 1.5-second scheduler slack in the F+confirm+grace+proof bound.
They cover bounded prompt/output handling, Blind behavior, silent tools,
termination escalation, descendant ownership, and escaped pipe teardown.
Notifications, outcome persistence, CPU sampling implementation, and safe
fall-through remain RL14/RL17, RL13, and RL15 respectively; none is wired here.

### RL12 review fixes

The supervisor module and export are explicitly Unix-only (as is the public
watch module). Blind is checked before feeding a drain batch, so a pinned
version mismatch cannot enforce a provider error from its first chunk.
The post-drain check also suppresses provider enforcement when the batch itself
latches Blind. Timeout and byte-stall flags require a live root: an already
successful exit cannot become a timeout while an escaped pipe holder delays
the bounded tail drain. TERM and KILL probes retain the first I/O error even
when later probes merely report a non-empty group.

Regression coverage includes source gating, first-chunk pinned provider errors,
a drain interrupted by an I/O error, a successful root with an escaped pipe
holder past its timeout, and transient EPERM at each signal probe.
