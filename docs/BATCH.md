# Batch files (`tools/apply_batch.py`)

A **batch** is an ordered list of ops the router replays against the keeper
HTTP API as the token's principal. The applier verifies the batch's
`source_sha256` against its `source` file *before any call* (live and
`--dry-run` alike). The sha256 pins the exact content of the source file
(its bytes); any change to the source file invalidates the batch. The batch
applies the ops as ONE atomic `apply_ops` call by default (all or nothing;
see "Atomic mode"); `--per-op` replays them one verb at a time and stops at
the first refusal.

## Format (YAML)

```yaml
batch: X7d-demo            # label
principal: router          # recorded owner; must match the token's whoami
source: plan/batch.md      # hashed source file (relative => batch file's dir)
source_sha256: <hex>       # sha256 of `source`; mismatch => refuse, exit 1
ops:                       # applied in order
  - {op: create, item: {<create_items fields>}}   # dict, or a list of dicts
  - {op: edit, key: project:ID, fields: {...}}
  - {op: legacy_state, key: project:ID, state: claimed|in_review, note: ...}
  - {op: add_evidence, key: project:ID, kind: decision, ref: {id: D-1}, repo: ...}
  - {op: close_on_decision, key: project:ID}      # batch_ref = abspath@sha256
  - {op: cancel, key: project:ID, reason: ...}
  - {op: priority, key: project:ID, priority: <int>}   # router set_priority, same transaction
```

Only the keys shown above are allowed (per op and top level); duplicate YAML
keys are refused. Keys are `project:ID`. A multi-item `create` lists every
item key in `plan`/`ok` lines. `--token-file` is required and never printed.

## Usage

```
python3 tools/apply_batch.py BATCH.yaml --url http://127.0.0.1:8787 \
    --token-file PATH [--dry-run] [--from N] [--per-op] [--allow-no-whoami]
```

`--dry-run` validates every op and prints the plan without any API call.
`--from N` resumes at op N (1..ops, else exit 2). A live run first prints
`acting as <name> (<role>)` from `whoami` and refuses on principal/role
mismatch. Whoami failures (including no-route) refuse with exit 2 unless
`--allow-no-whoami` is given, in which case a warning is printed and the
batch continues. Output: one `ok` per op, then
`summary: ... (applied <k> of <n>[, skipped <m> (before --from)])`,
exit 0; on refusal `op <n> <op> <key>: <code> <message>` plus
`resume with --from <n>`, exit 1.
`ref` must be a mapping; `kind`/`note`/`reason`/
`state`/`repo`/`key` must be strings, `fields` a JSON-encodable mapping; unhashable
YAML keys are refused (`invalid mapping key`); relative `source` resolves beside the batch file's real path (after symlinks). Token must be printable ASCII without whitespace; non-UTF-8 token files exit 2 with exception class name only.

## Atomic mode (default) and `--per-op`

By default the whole file (from `--from N`) is sent as one `apply_ops` call
(router only): the keeper applies every op in ONE SQLite transaction with the
same per-op auth/state checks as the single verbs, validates the whole graph
once at the end, and appends all events in that transaction. Later ops see
earlier ones, so "create a leaf, then link it from the goal" works in one batch
(it fails per-op). Any refusal rolls back every op: the output is
`op <n> <op> <key>: <code> <message>` (`n` in the batch file's numbering, even
with `--from`), `nothing applied (the whole batch was rolled back)`, exit 1; no
`resume with --from` hint, because nothing was applied. `close_on_decision` uses
the batch's hashed `source` as its `batch_ref`. Exit codes are unchanged.
`--per-op` keeps the old behaviour: one call per op, partial progress, resume
hint.

A refusal that no single op owns (the final whole-graph validation found a new
problem on an item no op of the batch touched, e.g. an edit that leaves another
item unreachable) is reported as `op - validate: <code> <message>`, with
`apply_batch` printing `op <first-op-number> ...` only when the keeper names no
op at all. An unparseable or short 2xx reply from the keeper exits non-zero
(`error: unreadable or short keeper reply ...`): the applier never prints
`applied N of N` on a guess.
