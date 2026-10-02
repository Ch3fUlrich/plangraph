# Plan graph data contract (shared by import, views, page, later the keeper) — v1

Python 3.12, stdlib only (+ pytest for tests). Package `src/plangraph/`; each file ≤ 400 lines. Tests in `tests/`.

## Item (one dict; the same shape in the import YAML and in the snapshot JSON)
| field | type | notes |
|---|---|---|
| `project` | str | owner project of the L1 that works it: `routing`, `autoos`, `server`, `workstation` (more later) |
| `id` | str | unique within the project, e.g. `T2-ONEPATH` |
| `key` | str | `project:id` — globally unique; **`depends_on` and `corrects` hold keys**. The import YAML may use bare ids for the same project; the importer normalises to keys. |
| `title` | str | ≤ 100 chars |
| `kind` | `lane`\|`gate`\|`goal` | `gate` = a phase gate from README (no work, closes when its lanes are done); exactly one `goal` per project that has one (global goal `routing:GOAL`) |
| `track` | str | `T0`…`T7` (plan v3 file) or a phase label; page groups by it |
| `owner` | str | who works it, from the lane text (e.g. `AutoOS L1-main`, `OPERATOR`, `prox`) |
| `state` | `open`\|`claimed`\|`in_review`\|`done`\|`cancelled` | `ready`/`blocked` are NEVER stored, only computed |
| `estimate_h` | number | hours; from the plan's estimates where given, else a stated default per lane complexity (flag with `estimate_src: default`) |
| `depends_on` | list[key] | prerequisites; cross-project allowed |
| `corrects` | key\|null | set only on a new item that corrects a DONE item |
| `done_criteria` | str | the plan's DONE (evidence) text, verbatim or trimmed |
| `evidence_kinds` | list | any of `commit+ci`, `run-record`, `semaphore-task`, `decision` (all required unless the list is `[any]`) |
| `operator` | bool | true when the plan marks it OPERATOR (human gate) |
| `priority` | int | 0 default, router-owned |
| `review_tier` | `standard`\|`high` | D-228 risk tier for reviews. Default at definition: `high` if `project == plangraph` or `id`/`title`/`track` matches a secret/key/security/gateway/omniroute/deploy/keeper/fence whole word, else `standard`. Router or the owning L1 may set it and raise `standard`→`high` while the item is not done; only the router may lower `high`→`standard`. |
| `source` | str | `docs/plan/T2-orchestration-core.md#T2-ONEPATH` (provenance, import only) |
Snapshot-only (keeper-written, never in the import): `epoch` int, `claimed_by`/`worker_run`/`reviewer_run` str|null,
`lease_until` iso|null, `evidence` list[{kind,ref,verified,at}], `model` str|null, `updated_at` iso, `corrected_by` list[key].
Terminal states: `done`, `cancelled`. `done` satisfies a dependency; `cancelled` does not (the validator flags it).

## Import YAML (directory `data/plan-v3/`: `T0.yaml` … `T7.yaml`, `gates.yaml`; each file ≤ 400 lines, each `{meta?, items: [...]}`; the loader merges them)
```yaml
meta: {source: routing docs/plan v3 @29a032d, generated_by: <model>, date: 2026-10-01}
items:
  - {project: autoos, id: T2-ONEPATH, title: ..., kind: lane, track: T2, owner: AutoOS L1-main, state: open,
     estimate_h: 4, estimate_src: default, depends_on: [T1-COMBO3], done_criteria: "...", evidence_kinds: [commit+ci],
     operator: false, source: "..."}
```
(Block style, one item per `- ` entry, for reviewability. Read with `yaml.safe_load` — PyYAML 6.0.1 is installed; it is
used only by import/export, never by views or the keeper core. Import validates with `views.validate` before output.)

## Snapshot JSON (`plan.snapshot.json`)
`{"generated": iso, "head": "<hash>|null", "items": [item+snapshot fields, all keys fully qualified]}`

## Views API (`src/plangraph/views.py`, pure functions over `list[dict]` items, no I/O)
- `index(items) -> dict[key,item]`
- `validate(items) -> list[Problem]` — Problem = `{code, key, msg}`: `dup_key`, `unknown_dep`, `cycle` (list the cycle),
  `depends_on_cancelled`, `no_goal`, `unreachable_goal` (only NON-terminal items must reach a goal; done/cancelled exempt),
  `bad_state`, `corrects_not_done`, `self_dep`.
- `ready(items, project=None) -> list[item]` — state `open`, every dep `done`; sorted by (-priority, -unblocks, estimate_h, key).
- `blocked(items, project=None) -> dict[key, list[key]]` — open/claimed items → their unmet dependency keys.
- `waves(items) -> list[list[key]]` — graphlib layers over non-terminal items; done deps count as satisfied; wave 0 = ready-now.
- `critical_path(items, project=None) -> {"keys": [...], "hours": float}` — longest remaining path by `estimate_h` over non-terminal items.
- `slack(items) -> dict[key, float]` — hours of slack per non-terminal item (0 on the critical path).
- `percent_done(items, by="estimate"|"count", project=None) -> float`
- `unblocks(items) -> dict[key,int]` — how many non-terminal items directly wait on it.
- `brief(items, project=None, n=10) -> str` — the agent text: one line per ready item `key | title | est Nh | unblocks N | prio P`.
Determinism: all outputs sorted; no randomness.

## Page (`src/plangraph/render.py`)
`render(snapshot: dict, vendor_dir: Path) -> str` returns ONE self-contained HTML string (snapshot JSON + `vendor/*.js`
inlined; no network). CLI: `python -m plangraph.render --snapshot S.json --out plan.html`. Behaviour: left-to-right layered graph
(dagre) with a virtual START node feeding items without deps and the goal at the right; colour by computed display state
(open, ready, blocked, claimed, in_review, done, cancelled); click → details panel (all fields + evidence + corrected-by);
project filter chips (multi-select) and track filter; DONE nodes collapsed per track by default (toggle); critical path
highlighted; touch pan/zoom; at width ≤ 700 px the default tab is a text list (Ready / Blocked-by / In progress / Done count)
and the graph is a second tab; light/dark via `prefers-color-scheme`; no external requests; read-only.
