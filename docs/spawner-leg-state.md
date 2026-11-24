# Provider limit evidence

Snapshot HTTP 401/403 responses are `auth_refused`, distinct from
`OmniRouteUnreachable`. The diagnostic includes the validated management gateway
URL, HTTP status, and caller's `PLANGRAPH_PRINCIPAL` (or `unknown` when unset),
never the key or response body. The adapter returns `down` with
`auth_refused` evidence and a detailed reason. Auth refusal clears
cached successful health and bypasses transport retries and recovery probes. A model absent from `opencode models --standalone` (run without the management token variables) is `not_listed` and is returned before any model-run probe; a discovery that fails (timeout, missing binary) is a `leg_state_snapshot_error`, which keeps the retry-then-probe recovery.
Routing candidates and saved run plans expose `auth_refused` and its evidence;
an already planned leg records the detailed refusal in its attempt skip reason.

A usable OmniRoute snapshot produces `provider_inactive` for an inactive
provider and `unreachable` for an advertised model without a provider entry.
A provider rate-limit window produces `limited` while its reset is in the future.

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
seconds/date, and body “resets in N seconds/minutes/hours” give an explicit reset, as does a Codex “try again at Nov 27th, 2026 8:24 PM” (host-local wall time), booked on every Codex leg.
At that reset the leg is eligible automatically. Without reset information the
leg is limited for ten minutes then eligible with `reprobe` evidence until a
real successful provider response clears it. Queries never update the database.
Provider-stated resets (`reset_at` parsed from headers or response/stderr text)
are booked and honoured uncapped until `reset_at` (including 49 h `opencode-go`
and 97–135 h `agy` resets); flat `down_s` fallbacks remain capped at 24 hours,
and liveness-probe or unparsed failure evidence still expires to `reprobe` after
six hours (24 hours for `agy` probe failures). Expired probe failures cannot
complete a fresh down streak, and a later probe failure never hides an active
provider reset.

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
disabled skips remain visible without changing the exhausted run's exit 4.
Missing models and inactive or unreachable providers also preserve exit 4
when the remaining eligible capacity is exhausted. A route with only those
skips, only static policy skips, or a request-size failure still refuses.

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
errors. The service plan exposes the detailed error on the matching candidate
as `leg_state_reason`. The launch boundary still retries the snapshot and probes
the leg when the retry fails. If routing recorded an error for a bare model
and launch uses a qualified state key, admission also rereads the bare model
after checking the qualified state. The archived attempts preserve the skip
labels from the run. The gateway response that produced the error is unavailable.

`src/plangraph/routing_support/quota_snapshots.py:_build` interprets a connection's
`testStatus` independently. Strings such as `refreshing` or `expired`, and an
explicit null, leave that connection unavailable while preserving other
connections in the snapshot. Invalid field types still report `bad_snapshot`
and take the retry and probe path above.

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

D-5966 / D-6310 agy quotas use agy:<model>, shared across effort variants of that model.
Hydrogen, Argon, Neon and Sonnet limits are independent. Gateway aggregate antigravity
quota snapshots do not suppress these native CLI models.
Legacy agy/<model>-<effort> events remain readable under the canonical key.
Explicit provider resets (including `agy` and `opencode-go`) survive the six-hour
probe-failure TTL and the 24-hour flat `down_s` cap until their stated `reset_at`.
The native Claude-harness Sonnet-high fallback reads and books its own
claude-sonnet-5-5-high subscription key. It never shares agy:claude-sonnet-5-5
quota evidence. CLI review quota results carry the serving harness, including
structured CLI limits and provider reset timestamps. Route
JSON carries the planning reader/clock snapshot rather than rereading global state.
Its agy Sonnet quota snapshot reads the agy key independently of the native leg.
Explicit-leg admission and provider-stop booking/fallback use the same
harness-aware quota key as dispatch. The helper lives in routing support;
`leg_probe.key` remains its compatibility alias.

Scheduled review probes (`--all-reviewers`, `caller='review-probe'`, source `review-liveness-probe`) probe every active leg the review route can offer (`planner.legs.routed_review_legs`, in `REVIEW_ORDER`) with a tiny `OK` prompt under `role='review'`; one failed review probe immediately books the leg `down` for `PLANGRAPH_LEG_DOWN_S` in the shared SQLite store, and `LegAdapter.leg_status` lets a stored booking beat a healthy gateway snapshot only when it is a `down` booking whose `evidence.caller` is `review-probe`, so failed review legs are not offered by the route until a passing probe restores them; `limited`/`expired` bookings and `down` bookings by other callers do not override the snapshot. While the leg is serving, `record_probe_fail` books `probe_failed_leg_serving` with no down window, so a failed probe then books nothing down. `pg-spawnd check-manage-key` checks key presence (`len > 0` without reading key bytes) and records one `missing_manage_key` alert under `state/alerts/manage-key.json` at probe time.

## AGY quota scope

An Antigravity 429, including `Individual quota reached`, applies to the model
that made the call and its effort variants. The event retains its account and
combo scope. Historical account-wide events cannot establish a model limit.
The scheduled review sweep re-probes limited AgY models at most once per hour
with a one-word `OK` request. A successful reply clears the model's limit.
Route quota states include the latest probe attempt as `last_probe_at`.

Launch admission rereads quota evidence rather than trusting an earlier plan. `QuotaGate` reads the configured local `QUOTA_SNAPSHOT_DB` read-only on every check, even when gateway liveness discovery is disabled; only management API reads are cached for 60 seconds. A provider pool is skipped while every account is at or below `PLANGRAPH_QUOTA_MIN_REMAINING_PCT` (default 1), until the earliest account reset. A reset-less row holds for 30 minutes from its timestamp or first observation.

`plangraph quota [--db PATH]` prints usage by provider, account and window with the
recorded observation time. `--db` selects an explicit read-only snapshot store.

Model discovery runs the harness found by `resolve_harness_binary` (`routing_support/clients.py`), never a bare `opencode` on the unit's PATH. A missing binary is a host fault: the leg state is `down` with reason `harness_missing: <name> <searched paths>`, not `unknown`, `unreachable` or a timeout.

A leg reads its provider state under the OmniRoute provider id of its model prefix (`omniroute_provider_id`, catalog seed: `mimo` is `xiaomi-mimo`). A prefix missing from the snapshot is `down` / `unreachable` with `evidence.looked_up_provider` naming the id looked up.
