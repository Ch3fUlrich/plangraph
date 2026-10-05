# Status card — plangraph-L1-main (stand-in session on coding.vm; L1 offline since 2026-10-04)
Updated: 2026-10-05 09:45 UTC | Brief: routing/docs/plan/briefs/plangraph-L1-main.md | Constraints: CONSTRAINTS.md | Handoff: docs/handoff/OPENCODE-L1-PLANGRAPH.md

## Phase
FINAL SAVE DONE (2026-10-05): the two parked WIP bundles from workst (D-621) are filed as local
branches in the L2 clone (see docs/CONTINUATION-2026-10-05.md). No code changed; worktree clean.
Keeper healthy (verify exit 0), md-sync + render timers live, page = OLD UI (operator OK for
GRAPH-UI still pending). plangraph 12.5 % done; only ready lane: plangraph:UI-TABLE-MODEL (TB1).

## Where the work state lives
- Branches on base gu-integ-ord 3cbfa08 in /home/s/.claude/jobs/7caaf7f5/tmp/ui-build (KEEP):
  parked/gu-g2b-step6-wip = 8dfa483 (steps 1-5 + partial 6b; 6 tests failing; 6a unwritten)
  parked/tb1-wip = 672eac3 (page_table_model.js 399 L FULL + page_table_url.js; tests missing)
- Resume briefs on the workst runner: C:\fleet\oc\plangraph\gug2b (g2b-step6a2/-6b2-ovh.txt).
- Resume points + first three actions: docs/CONTINUATION-2026-10-05.md (write this session).
- Accepted heads + rulings: docs/HANDOFF.md. Lane queue + rules: OPENCODE-L1-PLANGRAPH.md.

## Tooling set up 2026-10-05 (local, gitignored)
- serena: .serena/project.yml, LSP index (153 files), memory project_layout.
- graphify: graphify-out/graph.json (3773 nodes/9122 edges/147 communities); data/ kept out of
  the semantic pass. Use for navigation questions before grepping.

## Next (in order; routing gates apply)
1. routing: report final save; ask for the L2 slot to resume GU-G2b step 6 (6b then 6a,
   PROJECT_GAP=120 constants; round-4 browser check at 3 viewports).
2. Parallel slot: TB1 tests against parked/tb1-wip (new files only), then TB1b.
3. After G2b accepted: TB2 -> TB3a/TB3b -> TB4; G3b/G4b/G5; then P1 supersede rule (routing OK).
4. Later: PROGRESS-PANEL step B, MODEL-KINDS, stack note, worktree->main merge (closes
   K1-KEEPER via `Closes: plangraph:K1-KEEPER@8cdad08`; review-overdue alert is expected).

## Rules that bite most
record -> ACCEPT -> merge; L2 launches via routing; writers <= 850k in / <= $2 (D-593); seats
non-writer family; real-browser probe gates UI (D-437: no suites/browsers on coding.vm); files
<= 400 lines; never delete this worktree (services run it); revert = git revert only; no
push/remote without routing's OK. Full list: docs/process/L2-BRIEF-TEMPLATE.md.
