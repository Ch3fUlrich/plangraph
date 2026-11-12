# Flow watchdog (D-5050)

Build `cargo build --manifest-path rust/Cargo.toml -p pg-worker-core --bin
flow-watchdog --release`; install the binary in `~/.local/bin`. Copy the service
and timer from `deploy/watchdog` into the systemd user unit directory. Configure
`~/.config/plangraph/flow-watchdog.env`, then enable the user timer. Installation
and live activation are intentionally not performed by tests.

The checker consumes a required JSON snapshot, not an LLM. A host collector must
atomically refresh `~/.local/state/plangraph/flow-snapshot.json` before each tick:
`pace`, `budget` are weighted daily Claude pace and budget from
`tools/claude_usage.py pace`; `ci_failed`, `ci_total`, `infra_failed` count the
same CI window (infra failures are a subset of failures); `review_age_hours` is
the maximum age of an in_review item; `review_rounds` is the maximum per PR;
`critical_idle_hours` is the #690 critical-path idle measurement; `coding_vm_runs`
and `worker_idle` describe placement; `host_drift` compares expected host code refs;
`closes_recent` and `closes_previous` count equal-length daily windows.
The fields above are required; budget must be positive. Missing data queues a durable invalid-snapshot alert.
The seven condition groups produce 8 rule names: placement and host drift are separate.

Collectors may also supply `hosts` (an omitted list defaults to empty for compatibility).
Each entry requires `host`, `tmp_used_percent`, `root_used_percent`, `checkout_head`,
`origin_main_head`, `behind_commits`, and `age_hours`. `host-disk` alerts strictly
above 85% used on either `/tmp` or `/`. `host-code` alerts when the spawner checkout
is behind origin/main by more than 10 commits **or** 24 hours. The collector must
provide the ancestry-based behind count and age in hours since the first missing
origin/main commit, not the checkout commit's timestamp. Equal heads or a zero
behind count do not alert merely because the checkout is old. These two additional
conditions emit stable per-host identities with JSON-escaped identifiers; no Git or host access
is performed by the checker. Collectors must refresh origin/main before measuring.

## Busy sessions with quiet transcripts (D-5225)

The optional injectable `session_activity` snapshot reader adds one `busy-quiet`
condition. Omit the block only for collectors that do not implement this reader;
an explicit null/error is unknown, not healthy. Its input is:

```json
{"session_activity":{"now":10000,
  "sessions":[{"session_id":"session-1","role":"build","busy":true,
               "transcript_mtime":8740}],
  "runs":[{"run_id":"run-1","parent_session":"session-1","ended":9000}]}}
```

Implemented collectors must always emit `session_activity` on every snapshot,
using `null` for a failed or partial read of the whole block rather than omitting
it. Absence is reserved for legacy collectors without this reader; compatibility
means the checker cannot distinguish a contract-violating omission from legacy
non-implementation. Within a present block, retain failed evidence explicitly.

Collectors implementing this reader must also declare the top-level boolean
`session_activity_supported: true` on every snapshot. With that capability marker,
an omitted `session_activity` reports `busy-quiet unknown=reader`; an explicit
null does the same. The marker is optional for legacy snapshots, and an omitted
or false marker never suppresses a present block. This capability declaration
removes the omission ambiguity above for collectors advertising support.

`now`, transcript mtime and run `ended` use Unix seconds (fractions allowed).
The injected collector supplies the complete session list and spawner run records,
including each run's parent session and end time. `ended: null` means still live;
`runs: []` proves no runs, rather than failed enumeration. Busy sessions alert
strictly after 1200 seconds of transcript silence only when every run for that
session has ended. Unrelated live runs do not suppress the condition. No runs at
all also qualifies. The last ended run is the one with the greatest end time
(run ID breaks ties), or `none` when there are no runs. Idle sessions do not alert.

Ties select the lexicographically greatest run ID, independent of input order.
Rows with a literal `busy: false` are skipped before validating session ID, role
or transcript evidence. Missing or non-boolean `busy` cannot establish idleness
and reports `unknown=sessions`; busy rows still require a nonempty ID and role.

Rows are grouped by nonempty session ID before evaluating alerts. Identical rows
collapse to one; conflicting rows produce one `busy-quiet session="..." unknown=sessions`
identity, independent of row order, rather than retaining multiple quiet periods.
Groups whose rows are all explicitly idle remain skipped without validating details.

Each firing session queues one routing line, with JSON-escaped identifiers:
`FLOW-WATCHDOG: busy-quiet session="session-1" role="build" quiet_age_seconds=1260 last_ended_run="run-1"`.
`quiet_age_seconds` rounds fractional silence up to the next whole second, so
every firing line displays an age greater than the strict 1200-second threshold.
The existing durable active/queue mechanism keys this condition by session ID and
transcript mtime, not the increasing age or other display details. Repeated ticks
in the same quiet period without observed recovery do not re-alert; transcript progress starts a new period,
and an observed recovery clears the active identity just like existing rules.

Live-own-run suppression counts as an observed recovery and ends the standing
identity even when the session remains busy and transcript mtime is unchanged.
When that run ends, the unchanged quiet period may queue another alert. A live
run before the first firing never creates a durable skip identity: ending it
allows the first alert. Ordinary repeated ticks without recovery remain deduped.

Condition-local unknown evidence takes precedence over live-own-run suppression:
a live run does not hide unreadable transcript or run evidence. Its unknown line
remains deduplicated until the evidence recovers; with readable evidence, a live
own run suppresses the quiet alert and clears the standing identity as above.

Collectors must retain failed reads: null, missing or malformed transcript mtime
and run evidence produce the condition's own `busy-quiet ... unknown=transcript`,
`unknown=runs`, or `unknown=transcript,runs` line for each affected busy session, never a
healthy result or the unrelated `invalid snapshot` rule. A malformed run record
with a readable, nonempty `parent_session` invalidates run evidence only for that
parent, including a missing/invalid run ID or a bad/future end timestamp; other
sessions retain their own run evidence. A record with an unreadable parent, or
a missing/null/malformed run list, invalidates run evidence for every session.
Missing/null/bad
session lists, clock or reader blocks similarly report condition-local unknowns.
Unreadable run evidence queues a block-level `busy-quiet unknown=runs` line only
when no per-session line reports unknown run evidence, including an empty session
list or only idle sessions. This fallback retains failed reads without pairing
the same run failure with a redundant block-level line. Reader, clock and session-list validation failures return their
own unknown first, without evaluating later evidence blocks.
Unknown run lines caused by malformed records include `run_record_index=N`, the
zero-based offending record position (the first for that parent or block).
This diagnostic is display-only and does not change the standing unknown identity
when records move; unreadable whole lists have no record index to report.
Condition-local evaluation requires the outer snapshot to deserialize and pass
its existing validation. If unrelated required fields or host records are invalid,
the durable `invalid snapshot` alert supersedes all condition-local unknowns;
`session_activity` is not partially evaluated in that case.
Specifically, required-field and host-record deserialization errors, inconsistent
CI counts and a nonpositive budget return `Err` from the validation closure in
`flow-watchdog.rs`; only its `Ok` arm calls the quiet reader. The legacy findings,
host messages and stuck-condition messages in that arm are condition alerts,
not validation errors encoded as messages.
An invalid outer snapshot is not an observed quiet-session recovery: the checker
retains existing `busy-quiet` active identities without evaluating new evidence.
Thus a valid, invalid, valid sequence queues one quiet alert for an unchanged
period, plus the independent invalid-snapshot alert; a later valid recovery still
clears the quiet identity normally. Legacy condition identities are unchanged.
Timestamps must be nonnegative and no later than `now`. No transcript contents,
live session services, spawner files or LLMs are read by the checker itself; the
snapshot remains the injectable read boundary. This detects busy sessions waiting
on non-iterating shell loops as well as waits on already-ended children, without
adding a timer, cancelling work or making any live deployment change.

## Delivery

Routing uses the existing `PLANGRAPH_WATCHDOG_DELIVERY` JSON argv bridge contract
(see stall-watchdog.md): one JSON alert on stdin with recipient `routing` and one
message line per condition/session (the original eight rules share one line).
`PLANGRAPH_FLOW_PUSH` is an explicit
JSON argv push hook receiving the same event. No existing push implementation or
#690 implementation was found in this checkout; measurement reuse is an input
contract, not a claim of live integration. Both hooks must be wired by deployment.
Without a hook, events wait in a durable queue. Successful channels are
checkpointed independently. Each standing active identity is suppressed until
recovery or a new identity; delivery is at-least-once across a crash during acknowledgement.
Systemd prevents concurrent ticks of this service; do not run manual concurrent
instances against its state directory. Routing advances despite pending push;
the queue retains its head and newest 255 events, reporting overflow on stderr.

## Collectors, freshness and host integration
are deployment prerequisites, not supplied by this checker.
