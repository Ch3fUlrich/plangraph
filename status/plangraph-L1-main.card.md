# Status card — plangraph-L1-main (L1 project orchestrator; relaunch: Sonnet high, bypass, same worktree)
Updated: 2026-10-04 11:45 UTC | Brief: routing/docs/plan/briefs/plangraph-L1-main.md | Constraints: CONSTRAINTS.md | Handoff: docs/HANDOFF.md (READ IT FIRST)

## Phase
Pilot keeper live (593 items), K1 + P1 merged on worktree-proposal (NOT on main), plan page live behind Authelia, GRAPH-UI + TABLE (operator top priority, D-560/D-586) in build at workst.
No L2 running (all in-flight state is in files). Context cycled at routing's request (very long transcript).

## Live systems (coding.vm)
- Keeper 127.0.0.1:8787, config ~/.local/share/plangraph/keeper.json (repos: server=origin/IMPROVE (D-592), autoos/routing origin/main, plangraph main), PID daemon.pid (kill -TERM, never pkill).
- md-sync (P1) timer+path run with --keeper-config; gate LIFTED; manual runs need routing's OK. Status ~/.local/share/plangraph/md-sync.status.json.
- Plan page: plangraph-render.timer (5 min) copies the render INTO /home/s/code/plangraph/out/plan-cdn.html (same inode); prox nginx 192.168.178.73:8089 (firewall OPNsense only), edge plan.ohje.ooguy.com. Shows the OLD UI until the OPERATOR OKs GRAPH-UI.
- Operator preview (T3b fix6 line, real snapshot, local only): ~/.local/share/plangraph/preview/plan-t3b-fix6-preview.html (delivered to the operator by routing).

## In flight at workst (via the workst-L1-autoos bridge session; Taildrop files land in /home/s/fleet/taildrop/plangraph/ WITHOUT the plangraph__ prefix)
- GRAPH-UI GU-G2b (hulls, chip bar, overview default): steps 1-5 staged + browser-verified. STEP 6 history (workst-L1, 11:30Z): gug2b9 and 6a read ~740k tokens with ZERO edits (killed, ~$0.35 each); 6b (phone focus switch) wrote
  page_graph_phone_focus.js + tests, died at the 128k context (~$0.45); phone-test loaders fixed by a logged scripted edit. RUNNING since 11:27Z: gug2b6b2 (fix 6 failing tests; any changed old assertion, e.g. pan 66->44, listed with arithmetic) and
  gug2b6a2 (hull gap + font scope, code inline). I AGREED PROJECT_GAP=120 (80 touching + 24 strip + 16 clear; my 88 was wrong); page_graph_view.js line 188 `80` edited, never lines 12-13. ETA 12:15Z, then round-4 browser verification, delivery ~13:15Z.
  Then accept (review the changed-assertion list; all-hulls fit at 3 viewports), L2-LAUNCH (via routing) for G3b/G4b/G5 (heads in HANDOFF; pkg-graphui/BRIEF-GRAPHUI-4.md + L1-RULINGS.md).
- TABLE (D-586): lanes UI-TABLE-MODEL/VIEW/WIRE/PHONE. TB1 PARKED (WIP 650dcb5c, page_table_model.js 399 lines, no tests; continuation tb1-cont.txt) -> next OVH slot after step 6 (D-590/D-593), TB2 after; agy resumes tables when its quota returns.
  Vertex open-class overflow only while the workstation day < $8 (D-589; reopens after 00:00Z). TB3 SPLIT into TB3a/TB3b and TB1b (description + note search, router correction) are TEXT TO WRITE in the next L2 round (L1-RULINGS addenda 3+4); TB3 waits for G2b accepted.

## Next (mine)
1. P1 SUPERSEDE RULE lane (routing: after the GRAPH-UI wiring): a later D-row supersedes earlier rows for the same key (reviewed -> main sha); only the latest row attached as evidence; same-row conflicts stay invalid; agy round + seats; many Server keys wait for it.
2. PROGRESS-PANEL (package at workst, step B verification pending), MODEL-KINDS, stack note, merge worktree-proposal to main (own record), K1-KEEPER close needs `Closes: plangraph:K1-KEEPER@8cdad08` after it.

## Rules (full list: docs/process/L2-BRIEF-TEMPLATE.md)
D-543 agy first, Vertex medium only if the cap allows, $2/run; OVH writers per D-593; max 2 agy writers fleet-wide; D-562 neutral empty cwd for spawns from a private worktree; D-437 no suites/browsers on coding.vm
(except D-560's visual check); seats of non-writer families, writer family written by hand; a REAL-BROWSER probe gates UI code; record -> ACCEPT -> merge, no push/remote; anti-gaming line in every writer task.
## Lessons
Verify a Taildrop file is FILED (ls + sha256) before relaying; seats invent quotes (check every quote); lenient fake DOMs hide strict-mode errors; never kill by image name; manual md-sync waits for routing's OK.
- PROGRESS-PANEL step B verification: workst has no task; I owe a package/task (queued behind G2b round 4).
