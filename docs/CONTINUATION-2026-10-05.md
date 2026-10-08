# Continuation plan — plangraph, 2026-10-05 (final save + resume points)

Written by the stand-in session on coding.vm after plangraph-L1 went offline before its final
save (D-621 says the plangraph WIP bundles were collected from the workstation; they had not
been filed here until now). No code changed. This file records verified state and the exact
resume points; read it together with `docs/handoff/OPENCODE-L1-PLANGRAPH.md` (lane queue,
rules) and `docs/HANDOFF.md` (accepted heads, rulings).

## Verified state on 2026-10-05 (coding.vm)
- Keeper healthy: `pg admin verify` exit 0; chain + cache ok. Services live: md-sync timer+path,
  render timer (writes /home/s/code/plangraph/out/plan-cdn.html, old UI, operator OK for
  GRAPH-UI still pending). Live counts: 526 tasks fleet-wide, plangraph 12.5 % done,
  the only READY plangraph lane is `plangraph:UI-TABLE-MODEL` (TB1).
- Worktree `worktree-proposal` clean at 82bca46; last accepted base `gu-integ-ord` 3cbfa08
  lives in the L2 clone `/home/s/.claude/jobs/7caaf7f5/tmp/ui-build` (branch phase/ui-build).
  KEEP that job folder (it holds pkg-graphui + the accepted-head clones).
- Parked WIP bundles from workst (D-621) FILED 2026-10-05 into the L2 clone as local branches
  (sha256 verified before fetch):
  - `parked/gu-g2b-step6-wip` = 8dfa483 (base 3cbfa08; steps 1-5 + partial step 6b)
  - `parked/tb1-wip` = 672eac3 (base 3cbfa08; 650dcb5 = page_table_model.js 399 L +
    page_table_url.js 151 L, registered in render.py + test_render_cdn.py, `node --check` only)
- Tooling set up for faster navigation (local, gitignored):
  - serena: `.serena/project.yml` + LSP index (153 files) + memory `project_layout`
    (run via `uvx --from serena-agent serena …` in this folder).
  - graphify: `graphify-out/graph.json` (3773 nodes / 9122 edges / 147 communities, AST +
    semantic over docs; data/ excluded from semantic pass to keep plan data out of queries).
    Query from this folder: `graphify query|explain|path … --graph graphify-out/graph.json`.
- Remote (operator-ordered 2026-10-06): origin = github.com/Ch3fUlrich/plangraph (PRIVATE).
  main + worktree-proposal pushed. This supersedes the old "no remote without routing's OK"
  rule for this repo; keep the repo private — plan data must never land in a public repo.
- Keeper alerts to carry forward: `K1-KEEPER` review-overdue (its close is gated on the
  worktree->main merge with `Closes: plangraph:K1-KEEPER@8cdad08`, see HANDOFF item 4);
  PG-M2B/M3/M4 + PH-M2C-* have no descriptions (cosmetic; fix in a router batch, not solo).

## Lane queue (order per OPENCODE-L1-PLANGRAPH; resume points from the PARKED notes)
1. GU-G2b step 6 — resume from `parked/gu-g2b-step6-wip` (8dfa483):
   - 6b: 6 failing tests. 4 in tests/test_ui_phone_focus_switch.py (node script lacks the
     `_ctx` helper); 2 in test_ui_phone_overview2.py expect pan x 66, now 44 — derive the
     value from the label-box rule before changing the expectation.
   - 6a: desktop hull gap, nothing written. Agreed: PROJECT_GAP = 80 touching + 24 strip +
     16 clear = 120 as constants in page_layout.js (export PlanUI.PROJECT_GAP);
     page_graph_view.js line 188 passes explicit 80; label font cap 28 only in overview
     (page_graph_viewport.js updateSemanticZoom).
   - Then tests/test_ui_overview_geometry.py + mutation proofs + round-4 real-browser check
     (all hulls fit at 1440x900 / 1280x800 / 390x844, zoom >= 0.35). Resume briefs sit on the
     workstation runner (C:\fleet\oc\plangraph\gug2b): g2b-step6a2-ovh.txt, g2b-step6b2-ovh.txt.
2. TB1 — resume from `parked/tb1-wip`: write the TB1 tests + mutation proofs for the parked
   model (keeper `plangraph:UI-TABLE-MODEL` done-criteria: pure module + node-run tests,
   sort/filter/search under a stated ms budget on 1000+ synthetic rows, URL round-trip).
   page_table_model.js is at 399 lines = FULL: new code goes into a NEW registered file.
   Then TB1b (search covers description AND note, router correction).
3. TB2 (DOM view, strict fake DOM tests, probe 1440x900) — package `uibuild-GU-TB2-writer.tgz`
   is still on the workstation (plangraph__uibuild-GU-TB2-writer.tgz, Oct 4 10:23).
4. TB3a (tab/view switch/boot guard/URL) + TB3b (filter bar, selection->details,
   window.__planTable()) — only after G2b accepted. Max 3 files each.
5. TB4 (phone table, breakpoint 700 px, windowed rows, search via PlanUI.Query).
6. G3b (focus: single tap toggles; focus = double tap + Focus button), G4b (budget wiring,
   hover, legend), G5 (phone chips/breadcrumbs) — after step 6 accepted; heads in HANDOFF.
7. P1 supersede rule (needs routing OK): later D-row supersedes earlier rows per key; only
   latest row is evidence. Many Server keys wait for it.
8. Later: PROGRESS-PANEL step B verification, MODEL-KINDS, stack note, worktree-proposal ->
   main merge (own record docs/reviews/<date>-main-merge.md; closes K1-KEEPER with the
   `Closes:` line above).

## Process gates that apply to every step
- record -> ACCEPT -> merge; no push, no remote, no merge to main without routing's OK.
- L2 launches go through routing (`L2-LAUNCH <name> <brief> <cwd>`); writers <= 850k input,
  <= $2/run (D-593); OVH Qwen3.8-27B allowed for step 6 + TB1/TB2 while agy is out of quota;
  never Coder-30B; seats of a non-writer model family; writer family written by hand.
- Real-browser probe gates every UI step (never accept on green unit tests alone); browsers
  and full suites never run on coding.vm (D-437) — UI test files only (~6 s).
- Files <= 400 lines; anti-gaming line in every writer brief; check every seat quote.
- A writer failing twice on step 6 -> ask routing for the D-576 escalation.
- Never delete this worktree; the services run its code live. Reverts via `git revert` only.

## First three actions for the next session
1. Message routing: report the final save (bundles filed as branches, list above), ask for
   the L2 slot to resume GU-G2b step 6 (briefs already on the workst runner).
2. At workst: launch g2b-step6b2-ovh.txt against `parked/gu-g2b-step6-wip`; on acceptance
   fold 6a, then round-4 browser verification.
3. In parallel slot: TB1 test-writing brief against `parked/tb1-wip` (new test files only —
   the model file is full at 399 lines).
