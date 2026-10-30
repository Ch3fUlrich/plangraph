# Seats and writers come from `route`, never from the caller (D-3198)

Callers (L1 prompts, router rulings) do not name legs. For reviews:

```bash
python -m plangraph.spawner.cli route --card @card.json --role review --writer-family claude --json
python -m plangraph.spawner.cli spawn --card @card.json --role review --writer-family claude --seats "Review"
```

MCP: `plangraph_agent_spawn` with `role: review`, `writer_families`, `seats: true`
and no `model_or_combo`. `--seats` spawns one run per seat `route` picked and
returns `{"seat_group": "seats-<hex>", "seats": [records], "seat_coverage": [...]}`
(also saved as `<state>/seat-groups/<seat_group>.json`); each seat record's
`plan.seat_coverage` holds only its own entry with the `seat_group`. A seat
refused before launch still gets its own refused record. A seat that fails over
falls through only to a spare leg of a family no other seat holds.

* **Per writer part** (DECISIONS 3.2, D-3084): each writer family is covered by
  seats of other families (1 standard, 2 on high risk incl. one non-Claude),
  the same rule as `keeper/coverage.uncovered`. A seat may share a family with
  ANOTHER writer; a leg whose family is every writer's is excluded.
  `seat_coverage` lists which parts each seat covers (keeper names: Claude =
  `anthropic`).
* **Policy file** `src/plangraph/planner/route_policy.json` (or
  `PLANGRAPH_ROUTE_POLICY`): `banned_families` (kimi, deepseek), `off_families`
  (glm), `paused_harnesses` (`qodercli` review only, `agy` no seats). Skips are
  recorded as `banned_family` / `off_family` / `paused_harness`. Change the
  file, not code.
* **Inline-only legs** (`tool_mode: inline-only`: luna, mimo-pro) give no
  verdict in tool mode. Put `"diff_ref": {"repo": "/abs/path", "base": REV,
  "head": REV}` on the card; the spawner runs `git diff base...head` and
  inlines it for those legs only. Without `diff_ref` they are skipped
  (`inline_only_needs_diff`); a failing diff skips the leg
  (`inline_diff_failed: ...`) and the run falls through.
* **Registry data:** `catalog_models.harness` and `catalog_models.tool_mode`
  are catalog columns (schema, seed in `registry_columns.py` `PROVIDER_HARNESS` /
  `INLINE_ONLY_MODELS`, migration for older catalog files). An override whose
  row has no harness refuses with `override model missing registry data:
  harness` (the missing field is named).

Back: [SPAWNER.md](SPAWNER.md).
