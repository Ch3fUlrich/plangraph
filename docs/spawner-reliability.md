# Spawner reliability

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

## RL12: Unix event supervisor (P-R6)

`watch::supervise_events` accepts the already confined, jail-wrapped `Command`.
It does not reconstruct argv or environment and has no harness caller until
RL14. This is a host supervision slice, not Temporal worker adoption.
Rollback is `git revert`.

Separate bounded stdout/stderr readers run concurrently with the stdin writer;
EOF closes the prompt pipe. Only stdout enters the event parser. Both streams
refresh the byte clock and contribute to the capped private output tail. A
bounded queue and bounded drain prevent a producer from starving supervision.
Reader threads are not joined: escaped pipe holders cannot block the return beyond
the two-second drain deadline. The stdin writer uses nonblocking writes and a
cancellation flag; teardown joins it after cancellation (10 ms polling), releasing
the prompt even when an escaped process holds the pipe. No payload enters metadata.
Every call requires `Opts.timeout = Some(...)`, validated before spawn. Monitor
modes and Blind-only stall detection are not substitutes for a wall deadline;
even a continuously writing child with every monitor mode off remains bounded.

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

Regression coverage includes Unix builds, first-chunk pinned provider errors,
a drain interrupted by an I/O error, a successful root with an escaped pipe
holder past its timeout, and transient EPERM at each signal probe.

### RL12 part B review

`ChannelHealth::parsed` includes Unknown lines; the Known mirror delta therefore
subtracts Unknown exactly once. `mixed_kind_mirror_counts_both_lines` covers both
kinds in a single parser chunk without changing the correct counter semantics.
Unix gating is checked by compilation, not whitespace-sensitive source matching.
Stop retains the status returned by reap, including the terminal empty-group tick.
`Watched::io_error` records the first drain or writer failure without throwing away
the exit status, decisions, channel summary, or kill proof. Cancellation is sampled
only while the root is live. ESRCH from TERM/KILL is success; signal-zero still
reports absence to the sticky group-empty probe. EPERM remains an error.
Process fixtures use two-second F/confirm and three-second silence limits; the
confirm fixture emits at two seconds, with two seconds before contractual kill.
The escalation assertion retains a separate 1.5-second scheduler margin.

### RL12 supervision fix-round validation

The next reap reporting root exit triggers one group-empty probe during Stop,
so honored TERM does not wait out grace; surviving descendants still escalate.
The bounded 128-chunk queue is drained to Empty before monitor decisions.
Batch I/O failures mirror newly latched Blind health before returning the error.
Regression fixtures cover five-second grace with F+confirm+0.5-second completion,
a strong signal in queued chunk 70, and twenty bad lines followed by I/O error.

Stdin writer setup errors now enter the same bounded Stop state machine as
runtime I/O errors and remain visible in `Watched.io_error`; the monitor does
not tick after an I/O error. Natural-exit drain checks group emptiness at entry
and completion rather than polling every 10 ms. A real child exercises both
tree context and CPU signals through `supervise_events`, without a repository.
The escaped-pipe fixture has a three-second wall timeout and retains its
3.5-second teardown assertion. The existing `lib.rs` Unix module gate also
excludes watch tests from Windows all-targets builds.
