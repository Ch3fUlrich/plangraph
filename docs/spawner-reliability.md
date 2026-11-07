# Spawner reliability

## RL29: slice 1 — records, inventory and shadow (partial)

PR changelog: ships record bridges and read-only startup shadow inventory only;
settlement, RUN_ORPHANED/RUN_REATTACHED, re-attach, kill after max_s, Adopted RL12
backend, Reconciled capability and reconcile-to-reclaim sequence are next slice.
Mode `on` still refuses startup. Merge completion/C5 evidence is not claimed.

Run records recognize terminal `orphaned` status (semantic class ORPHANED),
with a null exit code rather than a manufactured failure code. Normal launch
records retain validated card `max_s` (1800 seconds when absent); finishing a
run preserves prior attempts and existing attempt-specific identity, start time,
deadline and outcome fields without copying legacy fields into completed attempts.
Attempt-specific values take precedence over legacy fields during inventory.

This slice implements read-only startup inventory in `shadow` mode, not mutating
startup reconciliation, adoption, recovery events/evidence replay, watchers or
slot reclaim. It must not be treated as RL29 completion or C5 evidence. Recovery needs
a follow-up production-line allocation, independent HIGH acceptance and live GO.
Rollback of these record bridges is `git revert`.

## RL13: weak CPU activity (P-R6)

`cpu::Sampler` is a pure thirty-second sampler with no daemon caller or switch.
RL14 binds its closure to the owned spawn identity in RL12 `Hooks.cpu`; no
child-id hook is assumed. Rollback is `git revert`.
The first read establishes a baseline. Errors, regressing clocks/counters and
Linux source transitions discard comparison evidence. Zero thresholds mean
one millisecond; large time jumps produce at most one weak signal.

Linux prefers a watchdog-validated run-scope cgroup `usage_usec`, otherwise
enumerates the owned process group under `/proc`. Per-(pid,starttime) deltas
exclude child cumulative times and baseline new members, preserving exited
members' accumulated CPU without counting PID reuse. Paths and group identity
are trusted supervisor inputs, never card authority. Empty groups add no CPU.
Unsupported platforms expose no Linux source; Windows Job Object accounting
remains the Windows supervisor's responsibility.

CPU refreshes only ACTIVE last_signal, not last_strong, first activity or
milestones; repeated CPU never satisfies F. Fixtures and owned local shell
children exercise real accounting with scaled scheduling, not thirty-second
sleeps. Historical harness-version fixtures/oracle parity, green CI and an
ACCEPT outside writer families remain external acceptance requirements; these
local tests do not claim them.

## RL27: inline review diff transport

Tools-mode injection defaults off; rollback is off. The operator environment
`PLANGRAPH_RELIABILITY_INLINE_REVIEW_DIFF` overrides `inline_review_diff` in
the operator config `plangraph/reliability.json`; `roles.review` overrides the
global setting. Shadow computes a boolean count without changing the prompt.
Inline-only review requires diff_ref and the NO-tools header even when off.
Repositories resolve through the self host in operator-owned hosts.json, never
adapter cwd. Raw diffs are capped at 262144 bytes before lossy UTF-8 decoding.
Only exact stdin carriers are injected; multiline .cmd/.bat launchers refuse.
Prompt persistence and hashing remain the final redaction step. No workspace
cleanup or verdict classification changes are included.

External ACCEPT and the live tools-mode seat gate remain operator-only checks;
local synthetic tests do not constitute either gate. Keep the switch off at merge.

## RL19: read-only research reports

Reports include TOOL_CALL_ONLY floor counts per launch, sibling-OK within 30 minutes
per monitor kill, and later-attempt OK recovery per classified non-OK attempt.
Each exposes count, denominator and fraction (null for an empty denominator).
Recovery and sibling searches use full corrected history, including outside the window.

`pg-spawnd health --state PATH --json` reads available history. `health report`
requires explicit RFC3339 `--since` and `--until`, selecting [since, until).
`--json` suppresses the human summary. `--baseline` atomically stores a private
`reports/baseline-<since>-<until>.json` under the resolved state directory.
It never writes the private historical baseline or enables routing. Rollback:
`git revert`. Outcome corrections precede window filtering; observations use
complete history, including prior-start holds and cleared no-time episodes.
MAX_TIME is neutral for strikes. Unknown legacy evidence remains unknown.
Legacy classification uses known exit/role facts; successful rows without usable
facts remain unknown. Gate skip fractions divide gate-less overrides by all launches.
The gate-known override denominator and unknown-gate count are separate metrics;
missing gates are never inferred as skips.
Recovery metrics retain full corrected history, and leg metadata is chronological.

The immutable operator baseline lives in a dated research directory in the private data repo.
Before RL14 on, compare the pinned window
2026-10-30 10:30Z through 2026-11-02 10:30Z: 641 runs, 155 run-hours,
77.3 unusable hours, 71 hangs, 43.8 hang hours, and every other stored metric.
Require relative error at most 5% for every nonzero metric and exact zero
otherwise. Store a local copy; never overwrite the historical measurement.
Seed timed and no-time parked observations for the operator check. No network
is used by the verb. Synthetic public fixtures are not historical measurements
and do not satisfy this live gate or independent ACCEPT.

## RL11: family floor and served-rate ordering

Pure `health::family_floor` and `health::order_legs` use first plan metadata per
leg id. Missing health is Closed; unknown metadata retains its evidence. A family
without Closed/Degraded peers floors exactly its first eligible heuristic OPEN
to available Degraded, preserving evidence and deadline. Hard reset facts and
provider overlays never qualify. Healthy peers are never demoted for a floor.
Review TOOL_CALL_ONLY retains review_unfit until a later review OK; other roles
still strike, open, and reset the non-verdict counter without setting review_unfit.
Stable order is tier, review-unfit (review only), then
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

## RL12: Unix event supervisor (P-R6)

`watch::supervise_events` takes the confined, jail-wrapped `Command` intact;
no harness caller is enabled before RL14. Rollback is `git revert`.
Separate bounded stdout/stderr readers and a nonblocking stdin writer run
concurrently; only stdout is parsed. Both streams refresh the byte clock and
feed the capped private tail. No payload enters metadata. Cancellation joins
only the writer (10 ms polling); escaped readers cannot block teardown.
Every call requires `Opts.timeout = Some(...)`, validated before spawn.
The bounded queue drains before monitor decisions, with 10 ms monitor ticks.
Cancellation wins over wall timeout, Blind byte stall, then event decisions.
Provider and I/O errors share Stop; the chosen reason is never replaced.
Teardown signals are calibration only (`late_signal_ms`). Tree hooks run at
start and every 30 seconds; CPU hooks supply signals, not a liveness bypass.
Parser counter deltas mirror Known and Unknown exactly once. First Blind is
sticky, emits `blind_events = 1` for RL14's `CHANNEL_BLIND`, and records only
would-warn/would-kill. Only Blind arms `Opts.stall`; parsing silence never does.
RL14 supplies the 600-second default; `Opts::default` remains unchanged.
The Unix-only backend owns a fresh pgid, probes before every signal, and
permanently latches ESRCH. Root reaping preserves descendant ownership;
adopted identity validation belongs RL29. Stop sends TERM once, waits grace,
sends KILL once, then bounds proof at 2000 ms. `Survivors` is kill-incomplete.
Root exit triggers an empty-group probe; natural exits also clean descendants.
Natural output drain is bounded at two seconds; terminal Stop drain at 100 ms.
Timeout, stall and cancellation require a live root. Blind suppresses provider
enforcement before and after each batch, including pinned-version mismatch.

The RL12 B1 foundation retains `Watched.io_error` for the first drain or
stdin-writer failure. The supervisor returns `Ok(Watched)` after
teardown, preserving exit status, decisions, channel summary, and kill proof;
RL14 reads the error field. Existing part A callers remain unchanged.

`killpg` rejects pgids at most one and the caller's own group. ESRCH from
TERM/KILL is success; signal-zero still reports absence to the sticky
group-empty probe. Other errors surface, and EPERM never proves emptiness.
Only a freshly spawned owned group enters the watch signal path. A reaped
leader does not relinquish surviving descendants; an unrelated sentinel
group remains alive when the owned descendant group is killed.

The remaining behavior and tests from the B1 foundation are implemented here.
Tests use real local children, synthetic parsers, integer-second event limits
and scheduler slack; they cover bounded I/O, Blind, escalation and escaped pipes.
Notifications/persistence, CPU sampling and safe fall-through remain RL14/RL17,
RL13 and RL15 respectively; no daemon or harness integration is enabled.

Inline review regression checks cover marker replacement, native stdin bytes,
unknown carriers, redaction and final-prompt hashes before dispatch, disabled Git
extensions, and cleanup restricted to live owned children. Batch launchers are
inspected case-insensitively; multiline native launchers remain supported.

Inline review refuses missing mandatory `diff_ref` before carrier validation.
Multiline prompts reject `.cmd`/`.bat` at any argv position; inline payload
marker/fence sequences are escaped. Timeout cleanup never waits for escaped
stdout holders outside the invocation-owned process group.

Exact-diff regression checks compare raw text and binary patch bytes with
`git diff --binary base head`. Tools-mode reviews without a prompt stay unchanged
when no diff is injected; mandatory inline reviews still require a safe carrier.
Prompt persistence ordering checks ignore source indentation.

Health research reports retain all recognized neutral outcomes in chronological
health replay and preserve their raw reset timestamps separately from effective
fold state. Unknown outcome strings count as missing classification evidence.
The tool-open savings population requires both open tools and a recorded
`s_eff_s`; open-tool rows without that timing evidence receive unknown bounds.
Invalid report windows are rejected before resolving or reading the state store.

The remaining behavior and tests require a follow-up size allocation, rather
than removal of tests to meet the 400-production-line limit. Rollback is
`git revert`; RL14 must not call this partial API.

Restart inventory now runs before listener acceptance when configured `shadow`.
Mode precedence is environment, role configuration, top-level configuration,
then `off`. Explicit `off` bypasses configuration and inventory. Inventory is
read-only and refuses malformed identities, timestamps and symlinked records.
Only missing-identity or Linux gone-PID `would_orphan` diagnostics are emitted;
missing state roots are empty inventories and are not created. Recorded
identities are not treated as verified liveness. `on` refuses startup until the
authority-checked backend, watcher, evidence replay and reclaim capability land.
The record settlement bridge writes semantic ORPHANED as lowercase `orphaned`,
with null exit status and a stable RUN_ORPHANED key; it is not yet called by
startup. No slot reclaim or process signalling is enabled by this partial slice.
# Adopted supervision bridge

The re-attach bridge provides a non-waitable child abstraction, a durable
`RUN_REATTACHED` marker, restarted first-activity/silence monitoring, and an
absolute max-time stop using the existing escalation path. Only proven emptiness
may produce the semantic ORPHANED result; an exit status is never invented.
These callable primitives are not yet connected to daemon startup. Verified
scope/Job Object adoption, CPU sampling, shadow diagnostics, and durable watcher
settlement remain incomplete. Reconciliation-gated reclaim is a separate slice.
Recovery watcher advancement persists attachment and warnings and settles only
proven-empty groups as orphaned with null exit status. Shadow diagnostics can
classify verified live no-scope leaders without signalling. Scope/Job adoption,
CPU sampling, and startup adoption/reclaim integration remain deferred.
