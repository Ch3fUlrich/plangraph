# Spawner reliability

## RL11: family floor and served-rate ordering

Pure `health::family_floor` and `health::order_legs` use first plan metadata per
leg id. Missing health is Closed; unknown metadata retains its evidence. A family
without Closed/Degraded peers floors exactly its first eligible heuristic OPEN
to available Degraded, preserving evidence and deadline. Hard reset facts and
provider overlays never qualify. Healthy peers are never demoted for a floor.
Review TOOL_CALL_ONLY retains review_unfit until a later review OK; other roles
are unaffected. Stable order is tier, review-unfit (review only), then
Closed/Degraded/HalfOpen/Open. Unfit is last within its tier, even after reprobes.
OPEN filtering remains RL18's responsibility.

`health_rates` counts only first attempts, last physical correction per
run/served-route/model. Default minimum is 20 samples and 50 percent usable;
below the minimum no demotion occurs. Only Closed becomes Degraded: rates never
create hard OPEN, reset evidence, or expiry. Authenticated served identifiers
must be supplied by RL18/R11, never guessed from requested models. RL10B's
provider window still outranks quota midnight when applicable.

No switch, daemon caller, or live routing is added. Rollback is `git revert`.
Hermetic fixtures are not recorded harness-version/oracle parity or live GO.
Those gates, green CI and independent HIGH acceptance remain required.

## RL17: derived evidence and review seats

Evidence defaults off. `reliability.json` accepts `evidence` values off/shadow/on,
with `roles.<role>.evidence` overriding the default and
`PLANGRAPH_RELIABILITY_EVIDENCE` overriding configuration. Shadow and on persist
derived data only; neither enables routing. Operators roll back with off and may
delete the derived evidence directory while writers are stopped.

Rows use a separate stable lock file, tolerate an incomplete final line, and
refuse malformed complete lines. Outcome corrections use last physical row per
run/attempt before window filtering. Observations are separate from outcomes;
canaries never become health incidents. Existing unclassified attempts are
counted and skipped, not guessed from status. Legacy leg_events stay in the
adapter until P-R11. Review seats remain enabled even when evidence is off.
Only supported complete result frames supply rendered review text; missing or
unknown output produces HALT, never a manufactured witness or finish reason.

## RL10b: observation-anchored health deadlines (P-R11 pulled forward)

`health_until::fold_with_provider` is a pure module without a production caller
or switch. Existing `health::fold` call sites remain source compatible and use
the legacy class-default deadlines with no provider overlay. Rollback is `git revert`.

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
retain the two 60-second reprobes followed by exponential OPEN. Only named
`canary` and `omniroute_*` observations are skipped; other kinds remain outcomes.
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

## RL26: exact references and live role admission
RL26 exact references accept registered ids or unique exact role-eligible models.
Ambiguous and fuzzy matches refuse, with deterministic bounded suggestions.
The read-only Python catalog bridge supplies id, model and roles; AdapterCatalog
caches successful reads for 60 seconds, keyed by state directory.

PLANGRAPH_RELIABILITY_LEG_REF overrides $XDG_CONFIG_HOME/plangraph/reliability.json
leg_ref (string or role map); default root is ~/.config (APPDATA on Windows), independent of state.
Default and invalid switches are off; warn/shadow observes without changing args.
On validates references before routing or spawning and canonicalizes args.leg.
RL26 is admission only: all modes retain the existing planner flow and its choice.
Selecting a leg by explicit reference belongs to RL20, the override gate.
Admission refusals retain validation refusal records and local host handling.
Non-null diff_ref on any effective role other than review refuses in every mode,
including dry runs, and names role review. The role rule is live at merge;
the reference switch defaults to off.

No external STD ACCEPT or historical live gate is claimed. Synthetic tests do
not satisfy either gate; independent acceptance remains required.

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

The RL12 B1 foundation retains `Watched.io_error` for the first drain or
stdin-writer failure. The follow-up supervisor returns `Ok(Watched)` after
teardown, preserving exit status, decisions, channel summary, and kill proof;
RL14 reads the error field. Existing part A callers remain unchanged.

`killpg` rejects pgids at most one and the caller's own group. ESRCH from
TERM/KILL is success; signal-zero still reports absence to the sticky
group-empty probe. Other errors surface, and EPERM never proves emptiness.
Only a freshly spawned owned group enters the watch signal path. A reaped
leader does not relinquish surviving descendants; an unrelated sentinel
group remains alive when the owned descendant group is killed.

The remaining behavior and tests require a follow-up size allocation, rather
than removal of tests to meet the 400-production-line limit. Rollback is
`git revert`; RL14 must not call this partial API.
