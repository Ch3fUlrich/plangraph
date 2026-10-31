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

Resolver results include state/evidence on every candidate (including excluded
limited candidates) and the selected leg. MCP catalog queries retain limited
legs for inspection. `views.leg_state(candidates)` exposes a pure web projection
without introducing a forbidden runtime spawner import. Planner selection runs
by default, with benchmark utility confined to each operator-selected tier.

The card-driven planner now reads this evidence through `plan_spawn` in
`planner/routing.py`. Both `limited` and `down` exclude a leg; expired and `reprobe` legs
remain selectable. The card's optional `account` and `combo` are passed unchanged
to the reader. The planner's provider-stop callback records only actual 429/402
responses with nonempty provider text, and can use an injected store for tests.

## Bounded writer liveness

`plangraph-spawn probe --leg ID [--leg ID ...]` or `probe --all-writers`
runs sequential tiny commit tasks in disposable local git repositories. Optional
`--timeout S` bounds each task; `--json` prints structured results. Exit is 0
only when every probe passes. MCP exposes `plangraph_agent_probe` with `leg`
(an array of IDs), `all_writers`, `timeout`, and `json` inputs.

A pass requires a changed HEAD with `ok.txt` in its tree. Failure records
`down` with a redacted reason and timestamp (source `liveness-probe`, status
503). `PLANGRAPH_LEG_DOWN_S` defaults to 1800; after that interval the state
is `reprobe`. A later successful probe clears it. State is local to this host's
state directory. `PLANGRAPH_LEG_PROBE_TIMEOUT_S` defaults to 180 seconds.
Before a normal writer dispatch, stale or missing probe evidence triggers a
probe; failure skips that leg and tries the next. Reviews and dry runs never
probe. `PLANGRAPH_LEG_PROBE_TTL_S` defaults to 1800 seconds; 0 disables
automatic probing. Explicit probes still run when TTL is 0.
