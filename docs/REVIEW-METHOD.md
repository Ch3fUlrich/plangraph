# Review method

Review policy lives in `plangraph.settings`, never in the keeper database.
Defaults: `mode = "batch"`, seats `{standard: 1, high: 2}`,
`distinct_families = true`, `family_source = "D-1704"`, `max_rounds = 3`
and `fixer_split_threshold = 10`.
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

## Enforcement boundaries

`source` is the most-specific contributing layer label (not a filename), not
per-field provenance. It never exposes an absolute settings path.
Policies are deliberately read per call: edits take effect without restart.
Explicit roots (phase planner `repo_root`, admission card `repo_root`) take
precedence over `PLANGRAPH_REPO_ROOT`. Otherwise the nearest `.git` ancestor of
cwd is used; without `.git`, cwd itself is the documented fallback.

Mode/scope admission and contextual fixer splitting are enforced. `seats`,
`max_rounds`, `distinct_families`, and `family_source` are validated and shown
but advisory: they do not configure routing seats, enforce family diversity,
limit rounds, or trigger replanning. No seat-count admission API is provided.
The sample `max_rounds` override above changes displayed policy only.

Batch ONE-review is a plan-time orchestration convention, not an admission
guarantee: admission has no batch count. The phase planner currently cannot
guarantee one review card: it reports existing keeper cards, does not synthesize
missing review cards, and retains multiple existing review cards. Consumers
must schedule one batch review per phase. Scope admission alone is not a count
check. This limitation is intentional pending orchestration support.

Explicit scope on a non-review spawn is rejected, including model/leg overrides;
explicit per-fixer is refused under batch policy regardless of default role.
Both stamped and explicit scopes are validated before choosing the effective
scope. Stamped scopes are evaluated against current project/phase policy.
