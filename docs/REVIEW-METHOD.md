# Review method

Review policy lives in `plangraph.settings`, never in the keeper database.
Defaults: `mode = "batch"`, seats `{standard: 1, high: 2}`,
`distinct_families = true`, `family_source = "D-1704"`, `max_rounds = 3`
(then replan), and `fixer_split_threshold = 10`.
The threshold controls connected-finding splitting; indivisible components may
exceed it. This setting does not add seat-count admission checks.

Set `PLANGRAPH_SETTINGS` to a JSON or TOML file. Relative paths resolve under
the nearest repository root. With no variable set, built-in defaults apply.
Resolution is field-wise: phase override > project override > global > defaults.
Seat maps merge by seat class. Invalid fields raise named `SettingsError`s.

```toml
[review_method]
mode = "batch"

[projects.demo.review_method]
mode = "per-item"

[phases."demo:PHASE".review_method]
mode = "batch"
max_rounds = 2
```

Inspect the effective value and its most specific contributing source:

```sh
python -m plangraph.settings show --project demo --phase demo:PHASE
```

The output is JSON. Phase plans also include `review_method`. Review cards carry
`review_scope = "batch"` in batch mode, or `"per-fixer"` in per-item mode.
Spawn CLI `--review-scope` and MCP `review_scope` accept these same scope values.
A review spawn requesting `per-fixer` under batch policy raises `SpawnRefused`
with reason/code `review_method_batch_per_fixer`, including audited overrides.
Per-item policy allows that scope. Include the card's `key` (project prefix) and
`phase` to resolve overrides at admission.
