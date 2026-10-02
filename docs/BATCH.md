# Batch files (`tools/apply_batch.py`)

A **batch** is an ordered list of ops the router replays against the keeper
HTTP API as the token's principal. The applier verifies the batch's
`source_sha256` against its `source` file *before any call* (live and
`--dry-run` alike). The sha256 pins the exact content of the source file
(its bytes); any change to the source file invalidates the batch. The batch
applies ops in order and stops at the first refusal.

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
```

Only the keys shown above are allowed (per op and top level); duplicate YAML
keys are refused. Keys are `project:ID`. A multi-item `create` lists every
item key in `plan`/`ok` lines. `--token-file` is required and never printed.

## Usage

```
python3 tools/apply_batch.py BATCH.yaml --url http://127.0.0.1:8787 \
    --token-file PATH [--dry-run] [--from N] [--allow-no-whoami]
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
