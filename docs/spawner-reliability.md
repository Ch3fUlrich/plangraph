# Spawner reliability

## RL12: backend/API foundation (partial)

The Unix-only `watch` module defines metadata-only hooks and results, an owned
child backend, and a clock-driven termination state machine. It is not called
by the harness. Commands are passed intact to the backend, which adds a fresh
process group and reuses the stall supervisor's spawn and signalling policy.
The stop path sends TERM once, escalates once after grace, reaps the root
without blocking, and requires ESRCH for emptiness. Permission errors cannot
prove a kill. Two seconds after escalation, unproven emptiness is `Survivors`.

This foundation does **not** complete SPAWNER-RL12-WATCH-SUPERVISE. Pending work:
bounded separate pipe draining and stdin delivery, the event loop and channel
aggregation, Blind shadow evaluation, source-priority selection, tree/CPU
sampling, calibration, EOF handling, and the required real-process matrix.
Owned groups remain signallable after leader reaping: Linux cannot reuse the
pgid while the group exists. Every signal first probes the guarded group;
the first ESRCH permanently latches emptiness, preventing later signalling.
EPERM is an error, not proof. Adopted children (RL29) retain identity checks.
`Survivors` carries the first signal, reap, or probe error kind, if any.
No production caller is enabled.

The remaining behavior and tests require a follow-up size allocation, rather
than removal of tests to meet the 400-production-line limit. Rollback is
`git revert`; RL14 must not call this partial API.
