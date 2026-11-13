# Provider limit evidence

`plangraph.spawner.leg_state` stores provider 429/402 responses in SQLite at
`$XDG_STATE_HOME/plangraph/leg-state.sqlite` (default
`~/.local/state/plangraph/leg-state.sqlite`). Tests inject a `LegStateStore(path)`.
There is no manual state setter; the CLI/MCP probe runs an actual commit task.

Transport integrations call `record_provider_response` with the actual HTTP
status, body, headers, and caller. `SpawnerRunner.provider_response` is the
gateway-response callback: provider stops record evidence, real 2xx responses
record probe passes. This callback must never be fed agent stdout, prompt text,
exit-code guesses, or arbitrary MCP arguments. The current subprocess gateway
does not expose HTTP responses to the parent, so its stdout monitor deliberately
does not infer limits; a gateway adapter must deliver actual responses through
the callback. A harness quota line (D-3257) or a failed liveness probe marks the leg limited/down with reason + time.

The exact `(provider/model, account, combo)` tuple defines scope; omitted
dimensions are not wildcards. Retry-After seconds/date, x-ratelimit-reset epoch
seconds/date, and body “resets in N seconds/minutes/hours” give an explicit reset.
At that reset the leg is eligible automatically. Without reset information the
leg is limited for ten minutes then eligible with `reprobe` evidence until a
real successful provider response clears it. Queries never update the database.
Failure evidence expires to `reprobe` after six hours, even when its booked reset is later;
expired probe failures cannot complete a fresh down streak.

Resolver results include state/evidence on every candidate (including excluded
limited candidates) and the selected leg. MCP catalog queries retain limited
legs for inspection. `views.leg_state(candidates)` exposes a pure web projection
without introducing a forbidden runtime spawner import. Planner selection runs
by default, with benchmark utility confined to each operator-selected tier.

The card-driven planner now reads this evidence through `plan_spawn` in
`planner/routing.py`. `limited`, `down`, and provider `expired` exclude a leg; `reprobe`
remains selectable. The card's optional `account` and `combo` are passed unchanged
to the reader. The planner's provider-stop callback records only actual 429/402
responses with nonempty provider text, and can use an injected store for tests.

Operator configuration may pair a `disabled` reason with a finite epoch `expires`.
The stop holds before that instant and expires at the boundary, using the planner's
injected `now` when supplied; it is never simultaneously an eligible leg and a
disabled skip. Stops with no expiry remain permanent. These local operator stops
do not override provider availability. A Go quota stop does not suppress a personal
credential leg; normal routing advances through the remaining eligible inputs.
When every otherwise eligible provider is limited/down/expired, static manual or
disabled skips remain visible but do not change the exhausted run's exit 4 into a
contract refusal. Static-policy-only or request-size failures still refuse.

## Bounded writer liveness

`plangraph-spawn probe --leg ID [--leg ID ...]` or `probe --all-writers`
runs sequential tiny commit tasks in disposable local git repositories. Optional
`--timeout S` bounds each task; `--json` prints structured results. Exit is 0
only when every probe passes. MCP exposes `plangraph_agent_probe` with `leg`
(an array of IDs), `all_writers`, `timeout`, and `json` inputs.

A pass requires a changed HEAD with `ok.txt` in its tree. Failure records
a redacted reason and timestamp (source `liveness-probe`, status 503), including
the scrubbed last 400 stderr characters on one line. The reason retains its
existing 300-character bound; only an explicit stderr excerpt adds up to 400
characters (plus the separator). Direct callers without stderr keep the old bound.
Two consecutive failures
book `down`; one is a `reprobe` hint. `PLANGRAPH_LEG_DOWN_S` defaults to 1800; after that interval the state
is `reprobe`. A later successful probe clears it. State is local to this host's
state directory. `PLANGRAPH_LEG_PROBE_TIMEOUT_S` defaults to 180 seconds.
Before a normal writer dispatch, stale or missing probe evidence triggers a
probe; failure skips that leg and tries the next. Dry runs never probe.
`PLANGRAPH_LEG_PROBE_TTL_S` defaults to 1800 seconds; 0 disables ordinary
automatic probing. Explicit probes still run when TTL is 0.

### Snapshot read errors (slice 1)

The cause in run-e99aae2287e541b48886d0f04e46c37e was
`src/plangraph/routing_support/leg_state.py:LegAdapter.leg_status`: its refresh
block caught an exception and replaced the cause with `unknown`.
Refresh now retains the exception type and redacted message in
`leg_state_snapshot_error: TYPE: MESSAGE`, without treating cached health as
a successful read. `src/plangraph/spawner/leg_probe.py:before_launch` retries
that read once before any snapshot-error skip. If it still fails, admission
probes the same leg, including reviews and TTL 0. A successful retry restores
ordinary admission; a successful probe admits despite the read error, while
a failed probe skips with both the underlying read error and probe reason. Read errors are logged even when a probe succeeds or the retry recovers; an ordinary successful probe also admits if its snapshot re-read fails again.
Infrastructure failure alone does not admit a leg with an unreadable snapshot.
Successful probe evidence, including passes during an unreadable snapshot, cannot suppress this recovery
probe. Booked provider limits and quota pauses still take precedence; dry runs
remain read/probe-free. This slice changes neither seat ordering nor route slots.
The default service reader retains its declared-state fallback for snapshot read
errors (as for the former `unknown` reason), preserving the detailed reason so
`before_launch` still retries and probes instead of mistaking fallback for a read.

A `running` record on the same model/harness wins over a failed probe when its
nonempty `live.log` grew within `LIVE_RUN_WRITE_WINDOW_S` (120 seconds).
Probes inspect only the owning service's `runner.state` (the resolved CLI/MCP
state directory), never a fresh environment fallback. Without that explicit
state, no serving override is claimed. A first nonempty append-log observation
uses its mtime; snapshots before and after the probe compare file identity and
size. Touching an old log without adding bytes does not renew growth evidence;
truncation, replacement, changing records and read races fail conservatively.
The event source is `probe_failed_leg_serving`, with status 503, the diagnostic,
and null `retry_after_s`/`reset_at`. It supersedes older probe failures without
claiming a probe pass or refreshing the successful-probe TTL; dispatch continues.
Missing, empty or older logs retain normal cooldown behaviour. Provider and
`exit_outcome` quota/usage-limit events remain authoritative even when a later
probe records serving evidence; this override applies only to probe failures.
The probe result adds `state` only for serving failures (or `host-not-ready`);
ordinary successes and failures omit the key, rather than returning null.
