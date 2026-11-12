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

`now`, transcript mtime and run `ended` use Unix seconds (fractions allowed).
The injected collector supplies the complete session list and spawner run records,
including each run's parent session and end time. `ended: null` means still live;
`runs: []` proves no runs, rather than failed enumeration. Busy sessions alert
strictly after 1200 seconds of transcript silence only when every run for that
session has ended. Unrelated live runs do not suppress the condition. No runs at
all also qualifies. The last ended run is the one with the greatest end time
(run ID breaks ties), or `none` when there are no runs. Idle sessions do not alert.

Each firing session queues one routing line, with JSON-escaped identifiers:
`FLOW-WATCHDOG: busy-quiet session="session-1" role="build" quiet_age_seconds=1260 last_ended_run="run-1"`.
The existing durable active/queue mechanism keys this condition by session ID and
transcript mtime, not the increasing age or other display details. Repeated ticks
in the same quiet period do not re-alert; transcript progress starts a new period,
and an observed recovery clears the active identity just like existing rules.

Collectors must retain failed reads: null, missing or malformed transcript mtime
and run evidence produce the condition's own `busy-quiet ... unknown=transcript`,
`unknown=runs`, or `unknown=transcript,runs` line for each busy session, never a
healthy result or the unrelated `invalid snapshot` rule. A malformed run record
invalidates the run evidence because its parent may be unknown. Missing/null/bad
session lists, clock or reader blocks similarly report condition-local unknowns.
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
the queue retains its head and newest 255 events, reporting overflow on stderr. Collectors, freshness and host integration
are deployment prerequisites, not supplied by this checker.
