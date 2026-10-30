# Provider limit evidence

`plangraph.spawner.leg_state` stores provider 429/402 responses in SQLite at
`$XDG_STATE_HOME/plangraph/leg-state.sqlite` (default
`~/.local/state/plangraph/leg-state.sqlite`). Tests inject a `LegStateStore(path)`.
There is no CLI flag, manual state setter, or MCP writer.

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
`planner/routing.py`. Only `limited` excludes a leg; expired and `reprobe` legs
remain selectable. The card's optional `account` and `combo` are passed unchanged
to the reader. The planner's provider-stop callback records only actual 429/402
responses with nonempty provider text, and can use an injected store for tests.
