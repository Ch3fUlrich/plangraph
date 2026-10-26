# Budget governor (design section 9, D23)

Daily metrics land in the keeper; a pure policy decides whether a new start may proceed.
It works with any model family: Claude is one leg (D-1533). The governor never blocks
answering questions, operator-started sessions, or a judge run the operator asked for.

## Pieces

| Piece | Where |
|---|---|
| Metric computation (transcripts, git) | `src/plangraph/governor/metrics.py` |
| Policy `allow(start_kind, initiator, now, ...)` | `src/plangraph/governor/policy.py` |
| Keeper reader/logger + CLI | `src/plangraph/governor/keeper_source.py`, `cli.py` |
| Planner start path | `src/plangraph/planner/start.py` (`guard_start`) |
| Keeper storage | table `metrics` (schema v11, PG migration 4), verbs `set_metrics`, `get_metrics`, `log_governor_override` |

## Metrics

Weighted tokens = `input + output + cache_write + 0.1 x cache_read`, deduplicated per message id
(`requestId`); landed change = first-parent merge commit; burn index = `day weighted / (weekly cap / 7)`.
Keeper table `metrics(day, metric, value, source, computed_at)`, one row per `(day, metric)`, upserted.

| metric | source |
|---|---|
| `claude_weighted_tokens` | transcripts |
| `claude_burn_index` | transcripts, needs a weekly cap |
| `landed_changes`, `weighted_tokens_per_landed` | git, derived |
| `provider_used.<provider>` | `--provider-usage` JSON `{provider: tokens}` |
| `rate_limited.<provider>` | `--rate-limited a,b` (value 1.0 = limited) |

Keeper access: `GET /v1/metrics?day=&since=&metric=` (any authenticated role); `POST /v1/set_metrics`
(router or system); `POST /v1/governor_override` (router, l1, system, spawner). Both writes are audited;
an override or fallback is one extra `audit_log` row with action `governor_override`.

## Context / cache report (`ctx`)

The daily job's JSON output gains a `ctx` section beside the unchanged `day`, `items` and
`posted` keys: `{"day": ..., "groups": [...]}`, one group per `(host, session_class)` for that
UTC day. The same JSON is also written to a file - `--out-dir DIR` gives `DIR/ctx-<YYYY-MM-DD>.json`,
and without the flag the report lands next to the transcripts the job read, i.e.
`<projects-dir>/ctx-<YYYY-MM-DD>.json` (written atomically, and `--dry-run` still skips only the
keeper upload). Nothing is posted for it - the keeper `metrics` table stays flat rows.

Dedup here is **first usage record per message id** (`message.id`, else the transcript line's
`uuid`): a later duplicate of that id is ignored, and a record without an id is never merged with
another. That is the reference scan's rule (`metrics.iter_usage_first`), and it deliberately
differs from `metrics.iter_usage`, which keeps the *largest* record per `(message id, requestId)`
for the weighted/daily totals, including pairs with a missing message id (or both ids missing).
Neither rule changes the other. Both transcript readers skip
missing or invalid timestamps, consistent with daily aggregation's exclusion of undated usage.
Both also skip a record whose `message.id` or `requestId` is present but not a string or int
(only that record; earlier versions dropped the rest of the file), and records are taken in
`(ts, file, line)` order, so which of two equal duplicates survives is deterministic.

| field | meaning |
|---|---|
| `calls` | deduplicated assistant calls in the group (first record per message id) |
| `weighted` | the existing `weighted()` sum (cache reads at one tenth) |
| `share_cache_read`, `share_cache_write`, `share_output` | each part of the *raw* token total `input+output+cache_read+cache_write` |
| `input_from_cache` | `cache_read / (input + cache_read + cache_write)` |
| `ctx_p50`, `ctx_p90`, `ctx_p99` | nearest-rank percentiles of the per-call context `input+cache_read+cache_write` |
| `share_over_200k` | fraction of calls whose context is over 200 000 tokens |
| `cost_usd` | price table below; cache reads cost `read multiplier x input price` |

`host` is `--host`, else `PLANGRAPH_HOST`, else `socket.gethostname()`. `session_class` is
`subagent` for a transcript under a `subagents/` directory or a record with `isSidechain` true,
otherwise `main` - refined to `router`/`L1`/`judge`/`worker` (or whatever the launch record
says) only by `--session-classes PATH`, a JSON `{session_id: class}` file; a session no launch
record names stays `main`, and a session name is never interpreted.

Prices in USD per million tokens `(input, output, cache_write)`, matched on the model id with
`DEFAULT_PRICE` `(3.0, 15.0, 3.75)` for an unmatched model: `opus` `(15, 75, 18.75)`,
`sonnet` `(3, 15, 3.75)`, `haiku` `(1, 5, 1.25)`. Cache reads bill at `0.05 x` the input price
for a model id containing `opus-5-5` or `sonnet-5-5`, at `0.1 x` otherwise.

```
python3 -m plangraph.governor metrics daily --date 2026-10-15 \
    --projects-dir "$PLANGRAPH_CLAUDE_PROJECTS_DIR" --host "$PLANGRAPH_HOST" \
    --session-classes /etc/plangraph/session_classes.json --dry-run
```

## Daily job

```
python3 -m plangraph.governor metrics daily --date 2026-10-15 \
    --projects-dir "$PLANGRAPH_CLAUDE_PROJECTS_DIR" --repo . --weekly-cap "$PLANGRAPH_WEEKLY_CAP_WEIGHTED"
```

Idempotent: a re-run of the same date upserts the same rows and rewrites the same
`ctx-<day>.json` report file (`--out-dir`, default the projects dir). `--dry-run` prints and
writes the file without posting.
Environment: `PG_URL`, `PG_TOKEN_FILE` (keeper client), `PLANGRAPH_CLAUDE_PROJECTS_DIR`,
`PLANGRAPH_WEEKLY_CAP_WEIGHTED`, `PLANGRAPH_PROVIDER_QUOTA` (`provider=tokens,...`),
`PLANGRAPH_HOST` (host name of the `ctx` report above when `--host` is absent; else the local
hostname).
Without a weekly cap no burn index is written and the governor fails open (`burn_unknown`).

Timer unit (install is a separate, operator step; this repo installs nothing):

```
# plangraph-governor.service
[Service]
Type=oneshot
EnvironmentFile=/etc/plangraph/governor.env
ExecStart=/usr/bin/python3 -m plangraph.governor metrics daily
# plangraph-governor.timer
[Timer]
OnCalendar=*-*-* 00:30:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
```

## Policy matrix

`allow()` returns a `Decision(allow, reason, override, fallback, ...)`. Unknown data fails open.

| start_kind | initiator | result |
|---|---|---|
| answer | any | allow `answer_always` |
| any | operator | allow `operator_initiated` (includes an operator-requested judge) |
| planner / expert / judge | agent | burn <= 1.0 and Claude not rate limited: allow; else fall back (below), else deny |
| worker_leg | agent | provider quota (any provider, Claude or not): used >= limit denies; provider rate limited denies |

Fallback (D-1533): when Claude is over 1/7 burn per day or rate limited, a new agent planner/expert/judge
start picks the first usable non-Claude provider from `fallbacks` (not rate limited, within quota), returns
`allow=True, fallback=<provider>`, and calls `on_fallback` (logged). Only when no fallback is usable is the start denied.
`guard_start` in `planner/start.py` derives the fallback providers from the routing legs (`Leg.family`, `claude_family` False)
and returns the matching `Leg`. An explicit `override` reason flips a remaining deny to allow and is logged once via `on_override`.

`python3 -m plangraph.governor check planner agent --fallback qwen,muse` prints the decision as JSON (exit 0 allowed, 1 denied).

## Follow-ups (not in this change)

- **Spawner wiring (AutoOS):** call `python3 -m plangraph.governor check <kind> <initiator> ...` (or `planner.start.guard_start`)
  before starting a planner/expert; honour `fallback` by routing to that leg. AutoOS is not edited here.
- **RT-P1-WORKER interface:** the Rust worker picks a leg; before a leg it should `GET /v1/metrics?metric=provider_used.<p>` and
  `rate_limited.<p>` (or call `check worker_leg agent --provider <p>`) and skip a denied provider. Left undocumented-in-code until that item.
