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
python3 -m plangraph.settings show --project demo --phase demo:PHASE
```

The output is JSON. Phase plans publish top-level `review_method` and `review_scope`
(`"batch"` in batch mode, or `"per-fixer"` in per-item mode). Review cards carry
`review_scope` only when `role == "review"`. The orchestrator passes the plan's
`review_scope` as `--review-scope` / `review_scope` on `role=review` spawns.
Spawn CLI `--review-scope` (on `spawn` only) and MCP `review_scope` accept these same scope values.
A review spawn requesting `per-fixer` under batch policy raises `SpawnRefused`
with reason/code `review_method_batch_per_fixer`, including audited overrides.
Per-item policy allows that scope. Include the card's `key` (project prefix) and
`phase` to resolve overrides at admission. A "per-fixer seat" is a review spawn with
`review_scope=per-fixer`.

## Enforcement boundaries

`source` is the most-specific contributing layer label (not a filename), not
per-field provenance. It never exposes an absolute settings path.
Policies are deliberately read per call: edits take effect without restart.
The guard reading the policy file twice (potential TOCTOU) and per-call re-read are intended:
policy changes take effect dynamically without daemon restarts.
Cross-project card key spoofing is not guarded at admission: card is trusted caller input,
with the same trust boundary as role.

Admission callers must set `PLANGRAPH_REPO_ROOT` or pass `root` (or include
`card["repo_root"]` when present); phase plans do not publish `repo_root`.
`enforce_review_scope` accepts `card["repo_root"]` when present.
Explicit roots (phase planner `repo_root`, admission card `repo_root`) take
precedence over `PLANGRAPH_REPO_ROOT`. Otherwise the nearest `.git` ancestor of
cwd is used; without `.git`, cwd itself is the documented fallback.

Only per-fixer refusal and fixer threshold are enforced.
`seats`, `max_rounds`, `distinct_families`, `family_source`, and ONE-review count are ADVISORY
(published in plan, not enforced): they do not configure routing seats, enforce family diversity,
limit rounds, or trigger replanning. No seat-count admission API is provided.
The sample `max_rounds` override above changes displayed policy only.

Batch ONE-review is an advisory plan-time orchestration convention, not an admission
guarantee: admission has no batch count. The phase planner currently cannot
guarantee one review card: it reports existing keeper cards, does not synthesize
missing review cards, and retains multiple existing review cards. Consumers
must schedule one batch review per phase. Scope admission alone is not a count
check. This limitation is intentional pending orchestration support.

Explicit scope on a non-review spawn is rejected as `SettingsError`, including model/leg overrides;
any review_scope given when `role != "review"` raises `SettingsError('review_scope only valid for role=review')`
before policy refusal. Both stamped and explicit scopes are validated before choosing the effective
scope. Stamped scopes are evaluated against current project/phase policy.

## Policy refusals vs. malformed input

| Category | Condition | Reported as | Reason / code |
|---|---|---|---|
| Policy refusal | Batch policy in effect and `review_scope="per-fixer"` | `SpawnRefused` (exit code 2) | `review_method_batch_per_fixer` |
| Policy refusal | Settings file missing, unreadable, or invalid JSON/TOML | `SpawnRefused` (exit code 2) | `review_method_invalid` |
| Policy refusal | Settings file missing, unreadable, or invalid JSON/TOML (phase runner) | `Refused` | `validation` (message prefix `review_method_invalid:`) |
| Malformed input | Unsupported `review_scope` value (not `batch` or `per-fixer`) | `SettingsError` / `ValueError` | `review_scope: expected batch or per-fixer` |
| Malformed input | `review_scope` provided when `role != "review"` | `SettingsError` / `ValueError` | `review_scope only valid for role=review` |
| Malformed input | Unknown field name in settings override | `SettingsError` / `ValueError` | `<name>: unknown review_method setting` |

