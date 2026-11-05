# Spawner reliability

## RL26 groundwork only: exact leg references (P-R6)

The Rust resolver accepts exact registered leg ids first, or unique exact model
references eligible for the requested role. Ambiguous models refuse; bounded,
deterministic distance hints never choose a route. No provider family inference
or route-group data is used.

This is inactive groundwork, not an enabled reference-admission slice.
The existing Python route verb cannot accept a leg selector. Catalog model ids
are not necessarily registered leg ids. Activating admission requires resolving
both contracts without bypassing planner availability policy. No live canary or
historical gate has been satisfied; rollback remains off.

Switch parsing supports environment-over-file precedence and off by default;
warn/shadow means observation only. The pure loader rereads the injected config
path, so a subsequent call sees updates immediately. Runtime wiring, cached
catalog admission, and bounded warning deduplication remain unimplemented.

Rule 11-05 is enforced independently: non-null card or top-level `diff_ref`
with effective role implement refuses before routing, including dry runs,
with `diff_ref requires role review; role implement is refused`.
Explicit review and absent/null references retain existing behavior.
The injectable admission helper preserves selectors in off/warn and atomically
canonicalizes them in on, but is deliberately not runtime-wired until the
catalog/route prerequisites are available. No claim of complete RL26 admission
or external STD acceptance is made.

### Re-scoped acceptance checklist

- Pure exact resolver, harvested fixtures, and injectable admission tests only.
- Independent rule 11-05 refusal before the route adapter, including dry runs.
- V5 production catalog bridge is removed from this groundwork checklist.
  `LegCatalog`, `AdapterCatalog`, the 60-second cache, and the Python read-only
  catalog verb belong to the subsequent production-admission slice.
- Runtime switch activation and shared MCP/override resolution are deferred;
  the dead-code allowance denotes this intentional boundary, not activation.
- Commit-2 shared-resolver and invalid-reference adapter tests remain required
  for that subsequent slice; helper tests do not establish production wiring.
- No STD ACCEPT or historical live-gate claim; external seats remain required.
