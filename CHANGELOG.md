# Changelog

Merged and pushed changes only (D-630 "plans follow reality", D-653). Newest first.

## 2026-10-08

- **Activity page (D-696):** keeper snapshot carries 31 days of state events (b8c731b); header page Activity with
  24 h / 7 d / 30 d / custom ranges, counts + lists per project (workst browser PASS). Render drops whole-line JS comments.
- **Operator waits (D-677):** keeper item field `wait` = answer|task (537d063); md-sync sets it from QUESTIONS.md,
  imports priority-table rows and suffixed OS ids (f7e59d1); page outlines items/phases/projects amber (answer) or
  violet (task), legend, Needs You badges, dependent tooltip (1f1d044, 2cd6d37). Decision text on the Decisions
  page (8c18e56). md-sync fetches all keeper repos first; workst-gen repo registered.
- **Page (later 2026-10-08):** Q&A answers update counts and row badges live (797c428, 2d53adb); URL hash keeps
  collapse/focus/filter state (59ff024); tests for table view, Q&A page, sidebar resize (f06ceb6); page_side.js /
  page_ui.js split under 400 lines + size guard (ef4c67f); Decisions table reads keeper ref objects (2d53adb).
- **Page (new UI live on plan.ohje):** opens on the collapsed project overview fitted to all hulls; data-guarded
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
