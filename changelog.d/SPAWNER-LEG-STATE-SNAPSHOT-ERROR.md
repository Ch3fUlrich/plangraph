SPAWNER-LEG-STATE-SNAPSHOT-ERROR (D-5654; D-2739/D-5715): retry a failed leg snapshot once, then probe the same leg instead of silently skipping it; a failed probe retains the scrubbed exception type/message.

The cause in run-e99aae2287e541b48886d0f04e46c37e was the refresh block in
`src/plangraph/routing_support/leg_state.py:LegAdapter.leg_status`:
`except Exception: self._error = 'unknown'` silently discarded snapshot errors.
The replay proof is owned by plangraph:SPAWNER-LEG-SNAPSHOT-REPLAY-PROOF.

Synthetic tests cover snapshot read error -> retry -> probe, retry recovery without a probe, underlying errors in scrubbed failure reasons, infrastructure failure without leg-failure bookings, and selection of a witnessed non-writer leg before an unwitnessed Opus leg.

Empty-snapshot rejection is limited to the HTTP provider (`OmniRouteState`); its connection-list contract and retry/probe recovery are tested. The same parameterized regression pins other providers returning `{}` to one snapshot read, `unreachable` for a listed leg, and no snapshot-error probe even on reviews with probe TTL 0. Missing providers in nonempty snapshots remain `unreachable`.

Remaining regressions cover unscoped and scoped provider-limit bookings, non-snapshot admission rechecks after probes, fresh successful evidence reuse, detached error details, omitted absent scope, helper-import isolation, type-only retry warnings, and path/secret redaction. Known limits and non-snapshot admission reasons are not overridden by successful or serving probes.
