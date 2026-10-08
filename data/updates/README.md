# data/updates/ - ordered router op batches

A batch is one YAML file replayed in order by the applier as the named principal, one keeper verb call per op.
It is a data file only: no op is applied by reading it.

```yaml
batch: 2026-10-01-router            # batch id
principal: routing-00               # the principal the applier acts as
source: /path/to/source.md          # the router's markdown the batch was converted from
source_sha256: <64 hex>             # the applier recomputes batch_ref from source + sha
ops: [...]                          # applied top to bottom; an op may only reference keys that exist at that point
```

| op | fields | notes |
|---|---|---|
| `create` | `item: {project, id, title (<=80), owner, track, kind: task, depends_on, review_tier?, evidence_kinds, done_criteria, estimate_h, estimate_src: default, priority, note}` | deps are `project:ID` keys; no `review_tier` = keeper default |
| `edit` | `key, fields: {...}` | router definition edit: `depends_on`, `review_tier`, `evidence_kinds`, `title`, `done_criteria`, `note`, `owner`; `depends_on` is the FULL new list |
| `legacy_state` | `key, state: claimed\|in_review, note` | only from `open` |
| `add_evidence` | `key, kind: decision\|commit\|semaphore-task, ref` | router-attached; `commit` also has `repo: server\|autoos` (sha exists and is an ancestor of main there) |
| `close_on_decision` | `key` | needs `review_tier: none`, `evidence_kinds: [decision]`, state claimed or in_review, a decision evidence attached |
| `cancel` | `key, reason` | |

`#` comments carry the context a verb cannot: `# NEEDS VERB: <key> <from>-><to>` marks a change for which no verb exists
(nothing is emitted for it).

Closing a legacy item by decision is always: `edit {review_tier: none, evidence_kinds: [decision]}` ->
`legacy_state in_review` (only if still open) -> `add_evidence decision` per D-id (+ `commit`) -> `close_on_decision`.

Splitting an item: create both halves -> close the done half -> `edit` every dependent (including `routing:GOAL` and the
gates) to point at the open half -> `cancel` the original with reason `split`.

Validate before loading: replay the ops on a copy of `data/plan-v3` and check no unknown key, no cycle, no dependency on a
cancelled item, and that every non-terminal item reaches `routing:GOAL`.
