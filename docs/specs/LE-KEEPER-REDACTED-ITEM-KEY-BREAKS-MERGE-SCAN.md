# LE-KEEPER-REDACTED-ITEM-KEY-BREAKS-MERGE-SCAN

Outcome. Define refuses a malformed or redacted item id, one malformed stored key no longer fails the merge scan of every repository, and a merge_scan_failed names the exception module and line in the reply and the keeper journal.

- plan_source. Keeper item plangraph:LE-KEEPER-REDACTED-ITEM-KEY-BREAKS-MERGE-SCAN, 2026-11-25 01:26Z, priority 10, D-6868, D-6874.
- base commit. 443bf493af86a2ccd1d2859fb4e1d52eceea522d
- card SHA-256. c2ae36652ecfea3834a05b537f90d5e96e79c99c2120d748c7745e646440fc5e
- tree SHA-256. 1bce342869288787870babb4d1ef984c75f289357c7b7eeee54d20261d8da35e
- evidence_after_deploy. After the keeper restart onto the merged head: `plangraph merge-close` (dry run) on coding.vm ends with an empty `scan_errors` or only non-`merge_scan_failed` reasons, and any stored malformed key appears once in `listed` with `evidence_unverified: malformed_key`. Record the run line in the item evidence.

## Checked against base

- The crash is still on main. `_split` returns `(None, None)` for a key failing `_KEY_RE`; `indexed_work` then evaluates `bare.lower()`. [src/plangraph/keeper/context_sources.py:39,99-104,416-436]
- `warm_merge_scan` calls `_warm_merge_candidates`, which calls `indexed_work` for every candidate in every project, and turns any exception into `merge_scan_failed: <class>` for the whole project. [src/plangraph/keeper/store_runs.py:312-345]
- `indexed_parts` does not raise for `bare=None` (`parts.get(None, ())`), but returns parts filed under nothing useful. [src/plangraph/keeper/context_sources.py:407-413]
- Define never checks the id shape: `_normalise` builds `key = f"{project}:{identifier}"` and runs only the credential guard on text fields. [src/plangraph/keeper/store_input.py:168-178]
- The automatic close lists a candidate with its reason when `_merge_repository(item)` raises `Refused`. [src/plangraph/keeper/store_close.py:712-735; src/plangraph/keeper/store_waiver.py:131-151]
- The refresh worker already logs one `merge_index_refresh` line to the `plangraph.keeper` logger. [src/plangraph/keeper/api_routes.py:469-476]
- File sizes against the 800-line gate: context_sources.py 793, store_runs.py 796, store_close.py 798 (not touched), store_waiver.py 771.

## Decisions

- D1. One key shape: `context_sources._KEY_RE`. Define refuses with `Refused("validation", ...)` when `project` or `id` would produce a key that fails it. When the id contains `[redacted]` (case-insensitive) the message is `<project>:<id>: id was replaced by a redaction marker; define the item with its original id`; otherwise `<project>:<id>: id must match [A-Za-z0-9_.-]+ and project must match [A-Za-z0-9_.-]+`. The check runs in `_normalise` right after the "needs project and id" check, so create, apply_ops create and import all get it. The upstream tool that redacted the id is not changed here.
- D2. A malformed stored key is skipped by the index readers: `indexed_work` returns `{"commits": [], "lanes": [], "prs": []}` and `indexed_parts` returns `[]` when `_split(key)` gives no bare id. The warm scan therefore finishes for every repository.
- D3. The key is reported alone: `_merge_repository(item)` raises `Refused("evidence_unverified", "malformed_key")` first when `_split(item["key"])[1] is None`. The automatic close lists it as `{"key", "state", "reason": "evidence_unverified: malformed_key"}`; every other candidate is processed normally.
- D4. Failure site. Add `errors.failure_site(exc) -> str` returning `"<ExceptionClass> at <file basename>:<line>"` from the innermost traceback frame (basename only: no directories, no values). `warm_merge_scan` stores `f"merge_scan_failed: {failure_site(exc)}"`. After a completed refresh the API worker logs one `log.warning("merge_scan_failed project=%s %s", project, reason)` per project whose reason starts with `merge_scan_failed`.
- D5. Legacy rows are not rewritten by this item; removing or rehoming the stored `plangraph:[REDACTED]` row is a router data step after deploy.

## Entry points

- E1. `Store.create_items` / `import_items` through `store_input._normalise`.
- E2. `Store.warm_merge_scan` and `Store.close_merged_auto` (frozen and unfrozen).
- E3. The merge refresh worker in `api_routes.py` and the `scan_errors` field of the close reply.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | `create_items` with id `[REDACTED]` raises `Refused` code `validation` naming the redaction marker; id `A B` or `a/b` raises `validation` naming the allowed form; nothing is stored. | X1 |
| B2 | E2 | With one item row rewritten to key `demo:[REDACTED]`, `warm_merge_scan(merge_candidates())` leaves `_merge_scan_errors` empty, and `close_merged_auto` closes the other merged items and lists only `demo:[REDACTED]` with `evidence_unverified: malformed_key`. | X2 |
| B3 | E3 | A scan whose index read raises `AttributeError` stores `merge_scan_failed: AttributeError at <module>.py:<line>` in `scan_errors` and the keeper log carries `merge_scan_failed project=demo AttributeError at`. | X3 |

## Verification

Write X1-X3 first; on the base X1 stores the item, X2 raises in the scan (`merge_scan_failed: AttributeError`), X3 shows no module or line.

- X1. `tests/test_keeper_malformed_key.py::test_define_refuses_a_redacted_or_malformed_id` (World + `defn`, as in `test_keeper_credential_guard.py`).
- X2. `tests/test_keeper_malformed_key.py::test_one_malformed_key_does_not_fail_the_scan_of_any_repository`. Reuse the `env`, `set_states`, `auto` helpers by importing them from `test_keeper_merge_close_auto`; land two merged items in the fixture repo, then `UPDATE items SET key='demo:[REDACTED]', id='[REDACTED]' WHERE key='demo:N'` on `env.conn` (a legacy row; define can no longer create it). Assert B2 for the frozen path (warm first) and the unfrozen path.
- X3. `tests/test_keeper_malformed_key.py::test_a_failed_scan_names_module_and_line`: monkeypatch `context_sources.indexed_work` to raise `AttributeError`, warm, read `close_merged_auto(...)["scan_errors"]`, and run the API refresh path through `door_util.Doors` (as `test_a_failed_scan_reports_its_reason_over_http`) with `caplog` on `plangraph.keeper`.
- X4. Held out: the card's `heldout_check` (merge close, credential guard and create tests) stays green unchanged.

## Scope and reuse

- R1. Reuse `_KEY_RE` / `_split` (no second key regex), the existing `Refused` listing path of the automatic close and the existing `plangraph.keeper` logger.
- N1. No change to the credential guard, to `store_close.py` (798 lines) or to how other refusal reasons are reported. No data migration.

### File budgets

Per-file caps are in the card `line_caps` and the TSV. `context_sources.py` and `store_runs.py` must stay at or below 796 lines: change lines in place (`store_runs.py` changes only the `except Exception` detail and its import line).

### Implementation

- store_input.py. Import `context_sources._split` (module-level `from . import context_sources`); after the project/id check, `if context_sources._split(key)[1] is None:` raise per D1 (a `:` inside the id already fails `_KEY_RE`, which allows one separator).
- context_sources.py. `indexed_work`: after `_, bare = _split(key)`, `if bare is None: return {"commits": [], "lanes": [], "prs": []}` (keep the `index is None` / `origin_stale` branch first). `indexed_parts`: compute `bare` before the `index is None` test and return `[]` when either is `None`.
- store_waiver.py. First statement of `_merge_repository`: the D3 refusal.
- errors.py. `failure_site` per D4; add it to `__all__`.
- store_runs.py. Line 337 uses `failure_site(exc)`.
- api_routes.py. In the refresh worker after the `merge_index_refresh` info line, the D4 warning per failed project.
- docs/KEEPER.md. In the merge-close paragraph: `malformed_key` listing, the `merge_scan_failed: <Class> at <file>:<line>` form, and the define refusal of a malformed or redacted id.
- changelog.d/LE-KEEPER-REDACTED-ITEM-KEY-BREAKS-MERGE-SCAN.md. One line.

## Open points for design approval

None.
