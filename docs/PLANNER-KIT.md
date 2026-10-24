# Planner kit

Code the planner uses to turn an item into a runnable unit of work: a validated **card**, a
**readiness gate**, a **route** (window class plus an ordered list of model legs) and a minimal
**context pack**. It lives in `src/plangraph/planner/`, is pure stdlib (plus `yaml` for batch
import) and does no model calls: the gate and the router cost $0. `service.py` glues the pieces to
a keeper `Store` (`put_card`, `get_card`, `set_state`, `ask_question`).

## Modules

| Module | Purpose |
|---|---|
| `card.py` | Card schema, collecting validator (`parse_card`), batch YAML import (`load_cards_yaml`, `cards_from_batch`, `strip_cards`, `apply_cards`) |
| `readiness.py` | `check_card` gate: every reason a card is not ready, as a `Verdict` of `Finding`s |
| `legs.py` | `DEFAULT_LEGS` (D25 order), `load_legs` (JSON override), `with_measured` |
| `routing.py` | `route_card` (window class + leg plan), `next_after_failure` (retry/rotate/promote/park) |
| `context_pack.py` | `build_pack`, `pack_budget`: write set then read set under a token budget |
| `service.py` | `gate_card`, `route_for_card`, `pack_for_card`, `import_batch`, `running_write_sets` |
| `shadow.py` | `route_with_shadow` (optional audit-only shadow answerer, D18), `record_outcome`, `read_rows`, `summarize` |

## Card YAML

Both forms may appear in one batch file. Bare ids use the batch `project` (or `default_project`).

```yaml
project: demo
cards:                      # form 1: top-level mapping, key = project:id or bare id
  A:
    outcome: add the thing
    write_set: [src/a.py, tests/test_a.py]
    read_set: [src/b.py]
    check_cmd: pytest -q tests/test_a.py
    window_class: 128k      # 128k | 256k | 1m | null
    risk_tags: []           # auth secrets money migration public-contract live-step
    estimate_tokens: 40000
ops:
  - op: create
    item:
      id: B
      title: second item
      card:                 # form 2: inline on the item (strip_cards removes it again)
        outcome: other thing
        write_set: [src/c.py]
```

Other fields: `exports`, `forbid_paths`, `smoke_checks`, `check_reads` (lists),
`heldout_check` (string), `check_kind` (`change`, the default, or `regression`).
Check metadata is persisted and included in worker claims; check inputs outside the write
set are protected alongside explicit forbidden paths. Gate inputs include smoke checks,
exports, held-out command, risk-tag presence, and the claimed base SHA.
Paths must be relative and without `..`.

## Gate codes (`readiness.check_card`, plus `no_route` from the service)

| Code | Meaning |
|---|---|
| `no_check` | `check_cmd` empty |
| `check_not_runnable` | dry run failed: executable or test path missing (nothing is executed) |
| `read_missing` | a `read_set` entry does not exist under the repo root |
| `write_overlap` | write set overlaps one of a running card in the same project |
| `estimate_missing` / `estimate_too_big` | no estimate / estimate above 0.55 x window |
| `leg_request_too_big` | estimate or context pack above the first leg's `max_request_tokens` |
| `no_risk_tags` | `risk_tags` is null (direct `check_card` calls only, see below) |
| `no_window_class` | window class unset |
| `bad_path` | absolute, `~`, drive or `..` path |
| `no_heldout` | `heldout_check` empty (only with `require_heldout=True`) |
| `no_route` | routing failed (service only; message is the route error) |

`gate_card` acts only on items in `open`, `needs_input` or `ready`. Ready: item set to `ready`.
Not ready: item set to `needs_input` and one open question (findings listed, no default, card
risk tags) goes to the planner queue; an identical open question is reused, so repeated calls
change nothing. The keeper stores `[]` for both absent and empty `risk_tags`, so `no_risk_tags` is
unreachable through the store.

## Routing and D25 leg order

Window class: the card's own, else the smallest class holding the estimate at 55% of the window
(128k = 131072, 256k = 262144, 1m = 1048576 tokens). Subtasks scale the estimate by
`1 + 0.1 * (n - 1)`. Legs are ordered by tier (trial, free, subscription, paid), then by position:

| # | Leg | Tier | Windows | Max request |
|---|---|---|---|---|
| 1 | qodercli Qwen3.8-Flash | free | 128k 256k | 100000 |
| 2 | opencode muse-spark free | free | 128k | 100000 |
| 3 | north-mini-code free | free | 128k | 90000 |
| 4 | laguna-s-2.1 free | free | 128k | 90000 |
| 5 | nemotron-3-super free | free | 128k 256k | 100000 |
| 6 | ovh gpt-oss-120b | free | 128k | 90000 |
| 7 | mimo/mimo-v2.6-flash | subscription | 128k 256k 1m | 200000 |
| 8 | agy gemini-3.8-flash | subscription | 128k 256k 1m | 900000 |
| 9 | agy sonnet-5.5 | subscription | 128k 256k 1m | 180000 |
| 10 | deepseek | paid | 128k 256k | 100000 |

Legs that train on data are dropped for cards tagged `auth`, `secrets`, `money` or `live-step`.
Any risk tag means risky review (2 seats), otherwise standard (1). Failure policy: rotate inside
the tier on `usage_limit`/`outage`, retry up to 3 attempts per tier, promote once, else park.

## Context pack layout

Plain text, deterministic: `# CARD <key>` header (outcome, check_cmd, window_class, risk_tags,
exports, forbid, smoke; never `heldout_check`), optional `# CHECK OUTPUT` tail (last 40-60 lines),
then `# FILE <path> (write)` blocks, then `(read)` blocks, each in a fenced block. Budget =
0.55 x window - 8000 tokens; tokens = ceil(chars / 4). A write file that does not fit raises
`PackTooBig` (route to a bigger class), as does a write file over the read-size limit;
read files over budget or size are skipped. Entries beyond the 50-file expansion cap are
listed in `skipped` as `(path, "entry_cap")`. Binary write files are skipped.

## Shadow hook

`shadow.py` (D18): `route_with_shadow(card, answerer=..., log_path=..., **route_kw)` routes with
`route_card` first and always returns that route unchanged - the shadow answer is audited, never
the decision. Off unless `PLANGRAPH_PLANNER_SHADOW` is `1/true/yes/on`; the JSONL log path is
`log_path` or `PLANGRAPH_PLANNER_SHADOW_LOG`. Each call appends one `decision` row (card_key,
rules, shadow answer, `shadow_error`, `agree` on the first leg id) and `record_outcome(decision_id,
outcome)` appends an `outcome` row joined by id; `read_rows`/`summarize` read them back, skipping
bad lines. Answerer exceptions are recorded and ignored, log-write errors swallowed; the answerer
gets a deep copy of the card and route, and `heldout_check`/`check_cmd`/`write_set` never reach
the log. The answerer is injected (fake in tests), so the kit still makes no model calls and no
store or schema change.

## Tests

```
python3 -m pytest -q tests/test_planner_*.py tests/test_split_make_public.py
```

`test_planner_service.py` uses the keeper `World` fixture, so it follows `PG_KEEPER_BACKEND` and
also runs in the Postgres CI job.

## Not here

- The planning-skill rewrite that emits cards.
- The live worker that claims `ready` items, spawns a leg and records attempts.
