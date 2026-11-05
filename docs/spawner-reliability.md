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
Linux signalling requires a captured, still-matching leader start identity;
missing `/proc` identity (including after reaping) refuses both TERM and KILL.
Other Unix platforms refuse signalling after observing/reaping leader exit.
Without a validated leader, live descendants are not signalled: bounded teardown
reports `Survivors`, never successful cleanup. Owned descendant membership across
leader reaping remains unimplemented. No production caller is enabled.

The remaining behavior and tests require a follow-up size allocation, rather
than removal of tests to meet the 400-production-line limit. Rollback is
`git revert`; RL14 must not call this partial API.
