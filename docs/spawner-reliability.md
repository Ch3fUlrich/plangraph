# Spawner reliability

## RL10b: observation-anchored health deadlines (P-R11 pulled forward)

`health_until::fold_with_provider` is a pure module without a production caller
or switch. Existing `health::fold` call sites remain source compatible and use
the same replay with no provider overlay. Rollback is `git revert`.

Hard reset classes select response resets at their receive timestamp, clamping
only response values to 24 hours with `reset::clamp_until`. Raw rows remain
unchanged; stored clamp flags are not authoritative. Integral times outside
the exact f64 range are unavailable reset evidence; defaults use saturating
arithmetic. Overlapping incidents retain freshest reset metadata, while their
replay timestamp remains the latest incident timestamp.

Provider deadlines are literal, including three-day values. `observed_s` is
first sight/change time, never poll time. Fresher observations win in either
direction, with response winning ties. No-time provider observations use
`observed_s + 86400` once. Expired values immediately permit HALF_OPEN; the
first real outcome at/after both observation and expiry settles that value.
Success closes, failure uses its own evidence, and explicit neutral outcomes
retain the two 60-second reprobes followed by exponential OPEN. Arbitrary
`kind` observations, including canary and OmniRoute diagnostics, are skipped.
An unchanged provider value is not reapplied after settlement. Clearing a
provider-only hold closes directly; independent older row failures remain.

Far-future response resets survive failed exception probes: without fresher
response evidence, reopen to the earlier of the original reset and failed
probe timestamp plus 24 hours. Provider values are never response-clamped.
Only requested leg/route/role and nonfuture outcomes enter replay.

RL11 owns family floor/order; RL18 owns usable freshness, cleared/unreachable
states and no-time episode persistence; RL23 owns probe single flight/canary
ownership. No HTTP, store, kill, cleanup, argv or signalling path is introduced.
Fake-clock fixtures are local evidence only: recorded fixtures per harness
version and oracle parity, green CI/ACCEPT outside writer families, two HIGH
non-writer families plus judge, and GO per SHA are required before any live
step. This module enables no live step.

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
