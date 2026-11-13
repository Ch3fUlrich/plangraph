# Daily paid-spend admission

`plangraph cost-gate refresh` computes a verdict from a complete gateway call-log
export. `cost-gate admit` recomputes immediately before every paid launch/retry;
it exits 0 only for `ok` or `near`, 1 for over/unpriced/truncated/invalid evidence,
and 2 for missing configuration or unavailable/stale/malformed input. No public
budget default exists. `--dry-run` computes without writing. Dry-run spawns do not
start paid work and therefore bypass launch admission.

The private config is `~/.config/plangraph/daily-gate.toml`, overridden by
PLANGRAPH_COST_CONFIG. It must have mode 0600 on Unix and an owner-only ACL on
Windows. Required TOML fields:

- `warn_usd`, `budget_usd`: quoted nonnegative fixed-decimal dollar strings;
  warn must be below the positive budget. Operator supplies both privately.
- `rows_file`: absolute path to the gateway export, owned by its exporter.
- `max_age_s`: optional export age limit, default 600, range 1..3600.
- `prices."provider/model"`: per-token quoted decimal `input`, `output`, and
  `cache_read` rates. Provider spelling is lowercase. All sources of paid spend
  must be represented. Subscription/free connections need explicit zero rates;
  no provider is silently exempted. A model with tokens and no rate blocks admission.

The exporter atomically replaces one JSON object with `day` (UTC YYYY-MM-DD),
`exported_at` (RFC3339), `truncated` (boolean), and `rows` (array covering the entire
UTC day, including every page). A page cap must set truncated=true. Rows use
`provider`, `model`, `timestamp` (RFC3339 or epoch seconds/milliseconds), and a
`tokens` object with nonnegative integer `in`, `out`, `cacheRead` counts. Empty
objects mean zero usage; absent counts default to zero. Missing/malformed blocks,
malformed counts and unknown token keys refuse admission.
Cached input is capped to input. Other days are ignored; bad dates are reported
and block. Failed calls carrying tokens still count. The exporter and its private
access credentials are operator-owned, independent of AutoOS. Incomplete exports
and partial JSON never become a zero-spend verdict.

Parsing, multiplication and addition refuse decimal precision/overflow loss.
Comparisons precede display; no floating-point money is used. The gate constrains
observed daily spend, not reservations for concurrent or in-flight calls.

State is private, atomic and locked at XDG_STATE_HOME/plangraph/daily-gate.json
and `.status` (LOCALAPPDATA on Windows). `cost-gate status` is the external
routerctl cost-line interface and refuses stale status. Admission never trusts
cached or migrated verdicts. PLANGRAPH_BIN selects the Rust gate binary for the
Python launch boundary; otherwise it resolves `plangraph` on PATH. Missing gate
execution refuses paid work. Catalog-paid overrides, explicit paid legs, unknown
billing tiers and the designated paid review seat all require the gate.

`cost-gate migrate [--dry-run]` imports the two old state files once without
overwriting new state. Originals stay for rollback. `cost-gate overlap-start`
records a durable start; `overlap-check` stays nonzero for the first 24 hours.
After that minimum overlap and operator verdict comparison, remove the old
cost-gate timer/service and repoint routerctl. These commands never change units.
