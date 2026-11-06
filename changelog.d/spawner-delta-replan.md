Whole-PR integration evidence (not claims about the last delta alone):

- `src/plangraph/spawner/cli.py:main` catches SpawnRefused and returns 2,
  except BenchmarkError, which returns 3.
- `src/plangraph/spawner/mcp.py:MCPSpawnerServer.handle_rpc_request` maps
  SpawnRefused to error data with exit_code 2 (BenchmarkError: 3).
- `src/plangraph/routing_support/leg_state.py:LegAdapter.leg_status` consumes
  models as either a callable or an iterable when refreshing its cache.

F5/F13 residuals: these implementation locations do not establish
exhaustion-specific CLI/MCP transport coverage or live adapter acceptance.
Those acceptance claims are unresolved, not closed by this note.

F7 remains open; no hung wiring is changed here. In
`src/plangraph/spawner/stall_watchdog.py:main`, the default hung_config is
None, so `src/plangraph/spawner/stall_probe.py:probe_run` does not reach a live
HungBoundary sampler. `src/plangraph/spawner/runner.py:SpawnService.cancel`
writes the run-level cancelled marker, and
`src/plangraph/spawner/runner.py:SpawnService.spawn.execute` stops the leg loop on
that marker. Reusing it would violate leg-scoped cancellation.
`src/plangraph/spawner/runner.py:SpawnService._save` persists output tails and
activity timestamps, not a host-owned token counter. Missing token evidence
cannot be treated as zero by
`src/plangraph/spawner/hung_capture.py:HungBoundary.probe`. Closing F7 needs
a host-owned usage sampler and a supervisor-consumed per-attempt stop/fallthrough
protocol; changing this note supplies neither and is not a fix or refutation.
