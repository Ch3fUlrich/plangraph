# Changelog

Merged and pushed changes only (D-630 "plans follow reality", D-653). Newest first.

## 2026-10-17

- **MODEL-NAMES:** `tools/names_backfill_dryrun.py SNAPSHOT.json [--json]` lists (never applies) the name backfill
  candidates of a plan snapshot: one sorted line per active item's `title_is_id` / `no_description` warning with a
  proposed title derived from the key and the head of the note's folded `description: ` line — read-only (no DB, no
  write verb, no network), exit 0 unless the input cannot be read (2); direct `views.warnings` tests cover both codes.

## 2026-10-16

- **RT-P1-GOVERNOR:** budget governor (`src/plangraph/governor/`, `docs/governor.md`): keeper `metrics` table (schema v11, PG migration 4) with `set_metrics`/`get_metrics`/`log_governor_override`, daily job `python -m plangraph.governor metrics daily`, pure `allow()` policy (never blocks answers, operator-started sessions or operator-requested judges; non-Claude fallback when Claude is over 1/7 burn or rate limited; provider quota throttle for worker legs; overrides and fallbacks logged), and `planner.start.guard_start`.

## 2026-10-15

- **PLANNER-KIT:** `src/plangraph/planner/` (cards, readiness gate, routing legs, context pack) plus `service.py` gluing it to the keeper store (`gate_card` moves items to `ready` / `needs_input` with one planner question, `route_for_card`, `pack_for_card`, `import_batch`); see `docs/PLANNER-KIT.md`.
- **PG-BACKUP-RETENTION:** `pg admin prune-backups --dir D [--keep N] [--daily-days N] [--apply]` trims ad-hoc backup files in a data dir (dry run by default, explicit allow-list, keeper files never touched); opt-in keeper `backup_prune` config hook, default off.
- **RT-P1-CONTRACT:** keeper contract 1.0 frozen in `docs/contract/` (prose, route reference, JSON schemas) with a storage-agnostic black-box suite in `tests/contract/` (subprocess keeper, or `PG_CONTRACT_URL` plus tokens). Keeper fixes: malformed `n`/`lease_s`/priority are 400 not 500; `verdict` `ok` must be a JSON boolean.

## 2026-10-11

- **QA-BACKFILL:** `tools/qa_backfill.py --questions F --out DIR` proposes choices for QUESTIONS rows without a valid Options block (`DIR/proposed-rows.md` + `DIR/review.md`, deterministic, writes nothing else).

## 2026-10-10

- **PG-AUDIT-META (D-1012/D-1013):** keeper read verb `audit_meta` — items in `open`/`claimed` missing `description`,
  `estimate_h` (NULL **or** `estimate_src: default`), `done_criteria`, `owner`, with `gate` and the operator kinds exempt
  from estimate and criteria; claims whose newest event is older than N days (default 3, injected clock) grouped by
  claimant (owner fallback) with the action to take — `close-by-D-row` on verified commit evidence (the snapshot's
  `merged` rule, no git call), `release` for an unreachable claimant or > 2N days quiet, else `renew`. Doors
  `pg audit-meta [--project|--stale-days|--skeleton DIR]`, `GET /v1/audit-meta`, MCP `audit_meta` (32 tools); router and
  L1 only, an L1 sees its own projects (another project via `--project` → 403). `--skeleton DIR` writes one
  `P.BATCH.yaml` per project of empty `op: edit` stubs beside its sha-pinned note, which
  `tools/apply_batch.py --dry-run` accepts (49e4e8d).

## 2026-10-09

- **SYNC-MD-IMPORT + md-sync Kind column:** QUESTIONS rows become question/operator_task, answered rows in
  Answers with D-ids (d931e98, cc138a6). **FU-X7-LOWS:** keyword prefix fix + evidence-shape integrity check
  (2119ae6). **PROGRESS-PANEL** (ec7b30d), **UI-TABLE-PHONE** (946cceb), **UI-MORE-NODE-EXPAND** (46abd5a).

## 2026-10-08

- **MODEL-KINDS (D-231/D-292/D-656):** item kinds question/decision/operator_task (operator/wait defaults), fields
  layer/role/agent_id/phase_kind/fixes, evidence kind `error`, router verb `answer` (event `answered`, locks like a
  decision close; old DBs get their lock triggers rebuilt on open), reads `needs_you` / `answers`; MCP 28 tools
  (aeb0208, L2-mk + plangraph-L1, judge ACCEPT).
- **UI lanes (L2-ui) + keeper proxy files:** table created column from snapshot created_at, node budget 200 per
  expanded phase with a '+N more' node, render new-UI detection on content (97d0ded). Keeper proxy (not live):
  nginx sidecar with mutual TLS + edge allowlist, mint-certs.sh for the shared internal hop CA (b00ea79, D-759 A).
- **PG-FETCH-ON-DEMAND / PG-SPLIT-EXAMPLES-TEST / PG-CI:** keeper fetches a stale origin/* clone once per repo per
  60 s when commit evidence misses (b8ea51b); examples regenerated + README pointer reworded, full suite 0 failures
  (5c029fa); GitHub Actions pytest on push/PR (fffd19f, run 37850230244 green).
- **PG-MODELS (D-708 A):** keeper verb `record_run` (new role `spawner`, principal created) records run ids, writer
  model and seat models per item (e2b07d9, judge ACCEPT server-L1-routing); show/snapshot add created_at, done_at,
  reviewed, merged. md-sync status close_skipped carries the reason (c9edf6c).
- **Activity page (D-696):** keeper snapshot carries 31 days of state events (b8c731b); header page Activity with
  24 h / 7 d / 30 d / custom ranges, counts + lists per project (workst browser PASS). Render drops whole-line JS comments.
- **Operator waits (D-677):** keeper item field `wait` = answer|task (537d063); md-sync sets it from QUESTIONS.md,
  imports priority-table rows and suffixed OS ids (f7e59d1); page outlines items/phases/projects amber (answer) or
  violet (task), legend, Needs You badges, dependent tooltip (1f1d044, 2cd6d37). Decision text on the Decisions
  page (8c18e56). md-sync fetches all keeper repos first; workst-gen repo registered.
- **Page (later 2026-10-08):** Q&A answers update counts and row badges live (797c428, 2d53adb); URL hash keeps
  collapse/focus/filter state (59ff024); tests for table view, Q&A page, sidebar resize (f06ceb6); page_side.js /
  page_ui.js split under 400 lines + size guard (ef4c67f); Decisions table reads keeper ref objects (2d53adb).
- **Page (new UI live):** opens on the collapsed project overview fitted to all hulls; data-guarded
  width/height styles (no cytoscape mapping warnings); Settings reachable on phone; dead duplicate of 16 functions
  removed from page_boot.js (04b1d78). Phase filter uses phase||track, `state:` facet is case-insensitive, sidebar
  tables capped at 300 rows (f856375). Real-browser check PASS at 1440x900, 1280x800, 390x844.
- **Keeper + md-sync:** supersede rule: the latest router D-row wins a key's `Closes:` sha; same-row conflicts stay
  invalid and are reported (9a8fd66). Keeper config: plangraph ref = origin/main. Daemon runs as the user unit
  plangraph-keeper.service.
- **Sync:** main (a01c8ed: header pages, Table, Q&A, Projects tree, skill plangraph-planning) merged with the live
  tree; restored `views.warnings` (keeper alerts), dropped stale real-name fixtures, keeper e2e tests on the
  fictional projects (1286436, 8902d06).
- **Docs:** review record docs/reviews/2026-10-08-since-1006-seats.md; layered-planning gap table and task-size
  options docs/proposals/2026-10-08-layered-planning-gaps.md (operator chose size B, D-656).
- **Plan data:** 2026-10-08 router batches under data/updates/ (see data/updates/QUEUE.md).
