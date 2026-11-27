# LE-KEEPER-REDACTED-ITEM-KEY-BREAKS-MERGE-SCAN

Outcome. Define refuses a malformed or redacted item id, one malformed stored key no longer fails the merge scan of every repository, and a merge_scan_failed names the exception module and line in the reply and the keeper journal.

- plan_source. Keeper item plangraph:LE-KEEPER-REDACTED-ITEM-KEY-BREAKS-MERGE-SCAN, 2026-11-25 01:26Z, priority 10, D-6868, D-6874.
- base commit. 443bf493af86a2ccd1d2859fb4e1d52eceea522d
- card SHA-256. 915fafd7014b93e3adb1fc0483a0bfd6177933bdc0849f98197970eea45a2fb4
- tree SHA-256. 120690ecde255f2a84e946f38619bc623d949be3f8f994a8b09238e169071cae
- evidence_after_deploy. After the keeper restart onto the merged head: `plangraph merge-close` (dry run) on coding.vm ends with an empty `scan_errors` or only non-`merge_scan_failed` reasons, and any stored malformed key appears once in `listed` with `evidence_unverified: malformed_key`. Record the run line in the item evidence.

## Checked against base

- The crash is still on main. `_split` returns `(None, None)` for a key failing `_KEY_RE`; `indexed_work` then evaluates `bare.lower()`. [src/plangraph/keeper/context_sources.py:39,99-104,416-436]
- `warm_merge_scan` calls `_warm_merge_candidates`, which calls `indexed_work` for every candidate in every project, and turns any exception into `merge_scan_failed: <class>` for the whole project. [src/plangraph/keeper/store_runs.py:312-345]
- `indexed_parts` does not raise for `bare=None` (`parts.get(None, ())`), but returns parts filed under nothing useful. [src/plangraph/keeper/context_sources.py:407-413]
- Define never checks the id shape: `_normalise` builds `key = f"{project}:{identifier}"` and runs only the secret-text guard on text fields. [src/plangraph/keeper/store_input.py:168-178]
- The automatic close lists a candidate with its reason when `_merge_repository(item)` raises `Refused`. [src/plangraph/keeper/store_close.py:712-735; src/plangraph/keeper/store_waiver.py:131-151]
- The refresh worker already logs one `merge_index_refresh` line to the `plangraph.keeper` logger. [src/plangraph/keeper/api_routes.py:469-476]
- File sizes against the 800-line gate: context_sources.py 793, store_runs.py 796, store_close.py 798 (not touched), store_waiver.py 771.

## Decisions

- D1. One key shape: `context_sources._KEY_RE`. Define first refuses a `project` or `id` that is not a `str` (`validation: project must be a string` / `id must be a string`), then checks each component on its own: it must fullmatch `_KEY_RE` and contain no `:`. Messages name the field and never echo the value: an id containing `[redacted]` (case-insensitive) gives `validation: id was replaced by a redaction marker; define the item with its original id`; any other failure gives `validation: id must match [A-Za-z0-9_.-]+` (or `project must match ...`). In an apply batch the existing batch index in the refusal identifies the entry. The check runs in `_normalise` right after the "needs project and id" check, so create, apply_ops create and import all get it. The upstream tool that redacted the id is not changed here.
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
| B1 | E1 | `create_items` with id `[REDACTED]` raises `Refused` code `validation` naming the redaction marker; id `A B`, `a/b` or `a:b` raises `validation` naming the allowed form; id `7` (an int) or project `["demo"]` raises `validation` naming the field; no message contains the rejected value; nothing is stored. | X1 |
| B2 | E2 | Two scanned repositories (`demo`, `data`), one valid merged item in each, and one item row rewritten to key `demo:[REDACTED]`: `warm_merge_scan(merge_candidates())` leaves `_merge_scan_errors` empty; `close_merged_auto` closes the valid item of each repository and lists exactly one row, `demo:[REDACTED]` with `evidence_unverified: malformed_key`; this holds for a frozen (warmed) run and, on a fresh fixture, for an unfrozen run. | X2 |
| B3 | E3 | A refresh started over HTTP whose index read raises `AttributeError` publishes `merge_scan_failed: AttributeError at <file>.py:<line>` in the close reply's `scan_errors`, and the refresh worker logs `merge_scan_failed project=demo AttributeError at <file>.py:<line>` with the same file and line. | X3 |

## Verification

Write X1-X3 first; on the base X1 stores the item, X2 raises in the scan (`merge_scan_failed: AttributeError`), X3 shows no module or line.

- X1. `tests/test_keeper_malformed_key.py::test_define_refuses_a_redacted_or_malformed_id` (World + `defn`, as the existing keeper text-guard tests build them).
- X2. `tests/test_keeper_malformed_key.py::test_one_malformed_key_does_not_fail_the_scan_of_any_repository`. Reuse `env`, `set_states`, `auto` and `two_repos` from `test_keeper_merge_close_auto`; land `Plan-Item: demo:C` in the `demo` repository and `Plan-Item: demo:B` in `data`, then `UPDATE items SET key='demo:[REDACTED]', id='[REDACTED]' WHERE key='demo:N'` on `env.conn` (a legacy row; define can no longer create it). Parametrize over `frozen` / `unfrozen` so each mode gets its own fresh `env` (an apply in one mode would consume the valid candidates of the other); assert B2 in each.
- X3. `tests/test_keeper_malformed_key.py::test_a_failed_scan_names_module_and_line`: monkeypatch `context_sources.indexed_work` to raise `AttributeError`; through `door_util.Doors` POST `/v1/close_merged_auto` `{"refresh": "start"}`, join the `keeper-merge-index` thread and poll `{"refresh": "status"}` as `tests/test_keeper_close_merged_scan.py::test_watch_merged_open_alert_requires_two_nonzero_passes_and_clears` does, then POST a dry-run close; assert the file and line in `scan_errors` and in the `caplog` warning of logger `plangraph.keeper` (the worker's log, `src/plangraph/keeper/api_routes.py:469-476`).
- X4. Held out: the card's `heldout_check` (merge close, the text-guard key tests and create tests) stays green unchanged.

## Scope and reuse

- R1. Reuse `_KEY_RE` / `_split` (no second key regex), the existing `Refused` listing path of the automatic close and the existing `plangraph.keeper` logger.
- N1. No change to the credential guard, to `store_close.py` (798 lines) or to how other refusal reasons are reported. No data migration.

### File budgets

Per-file caps are in the card `line_caps` and the TSV. plangraph:LE-NO-KEEPER-VERB-FOR-AN-OPERATOR-ORDERED-STALE-CLAIM-AND-MERGED-CLEANUP also writes `store_waiver.py`, `api_routes.py` and `docs/KEEPER.md`, and plangraph:LE-NO-PROGRESS-ALARM-FOR-AN-IDLE-FLEET writes `docs/KEEPER.md`; the keeper's `write_overlap` gate runs them one after another, and the final caps of the shared files hold in either order (`store_waiver.py`: 771 + 4 here + 24 there = 799). `context_sources.py` and `store_runs.py` must stay at or below 796 lines: change lines in place (`store_runs.py` changes only the `except Exception` detail and its import line).

### Implementation

- store_input.py. Import `context_sources._split` (module-level `from . import context_sources`); after the project/id presence check, apply D1: type checks first, then per component `not _KEY_RE.fullmatch(value) or ":" in value` (reached as `context_sources._KEY_RE`).
- context_sources.py. `indexed_work`: after `_, bare = _split(key)`, `if bare is None: return {"commits": [], "lanes": [], "prs": []}` (keep the `index is None` / `origin_stale` branch first). `indexed_parts`: compute `bare` before the `index is None` test and return `[]` when either is `None`.
- store_waiver.py. First statement of `_merge_repository`: the D3 refusal.
- errors.py. `failure_site` per D4; add it to `__all__`.
- store_runs.py. Line 337 uses `failure_site(exc)`.
- api_routes.py. In the refresh worker after the `merge_index_refresh` info line, the D4 warning per failed project.
- docs/KEEPER.md. In the merge-close paragraph: `malformed_key` listing, the `merge_scan_failed: <Class> at <file>:<line>` form, and the define refusal of a malformed or redacted id.
- changelog.d/LE-KEEPER-REDACTED-ITEM-KEY-BREAKS-MERGE-SCAN.md. One line.

## Open points for design approval

None.
