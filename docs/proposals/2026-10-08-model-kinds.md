# MODEL-KINDS split (D-231, D-292, D-656 gaps 1/8/9/11), 2026-10-08, plangraph-L1

Item: plangraph:MODEL-KINDS. Gaps doc: docs/proposals/2026-10-08-layered-planning-gaps.md.

## Already there (no work)
- Operator waits: item `operator` + `wait` = answer|task (D-677), the page's Needs You / Q&A, md-sync closes by D-row.
- Corrections: item field `corrects` (new item points at the one it corrects), `corrected_by` in the snapshot.
- Spawner run-id link (gap 8): PG-MODELS `record_run` → `run_ids`, `writer_model`, `seat_models` (189c4ee).

## Lanes (each: migration via `_ADDED_COLUMNS` where a column is added, validation, create/edit, event fold +
## verify_integrity, importer/export, show/snapshot; copy how `wait` and `host` were added; files ≤ 400 lines)

| lane | what | tier |
|---|---|---|
| MK-FIELDS | optional item fields `layer` (L1/L2/L3), `role` (planner/implementer/reviewer/researcher/operator), `agent_id` (≤ 128, `[A-Za-z0-9._:@-]`), `phase_kind` (planning/work; gate items only, else refused). Page: `roleOf` uses `role` when set (fallback = today's title guess). | high |
| MK-KINDS | item kinds `question`, `decision`, `operator_task` besides lane/gate/goal (importer `_KINDS`, create validation, views). They block dependents like a lane. Create defaults: question/decision → `operator=true, wait=answer`; operator_task → `operator=true, wait=task` (explicit values win). | high |
| MK-ANSWER | router verb `answer(key, text, decision_id)`: item kind question/decision, state open/claimed/in_review; decision verified like `close_on_decision` (the D-row must name the key in `Closes:`); event `answered` {text ≤ 2000, decision_id} then the same final state/lock as `closed_on_decision`. HTTP + MCP + CLI-free. | high |
| MK-READS | read verbs `needs_you(project?)`: open items with operator=true or kind in (question, decision, operator_task), ranked by unblocks desc then priority; `answers(project?)`: done question/decision items with answer text + decision id + done_at, newest first; an answer whose item is named by a later item's `corrects` is marked `superseded_by`. Pure functions in views.py, thin store/HTTP/MCP doors. | standard |
| MK-FIXES-ERROR | item field `fixes` (key of the failed item; create refuses a 3rd open-or-done fix item for the same target, skill §5 max 2) shown as `fixes`/`fixed_by` in show/snapshot; evidence kind `error` = compressed error record {code ≤ 64, msg ≤ 500, run_id?, where?} (skill §6), recorded as unverified and never counted toward done/merged. | high |

Order: MK-FIELDS and MK-KINDS first (independent), then MK-ANSWER and MK-READS (need kinds), MK-FIXES-ERROR any time.
MODEL-KINDS closes when the five are merged (router D-row).

## Out of scope here
Fan-out verb (gap 3), toolchain block in briefs (gap 6), result schema checks (gap 12): AutoOS lanes.
