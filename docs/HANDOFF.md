# HANDOFF — plangraph-L1-main, refreshed 2026-10-04 ~06:30Z (for a relaunch; Sonnet/Opus, bypass, D-496/D-506)

Read in this order: CONSTRAINTS.md -> status/plangraph-L1-main.card.md (live state) -> this file -> docs/process/L2-BRIEF-TEMPLATE.md.
Talk to routing (`routing`), workst-L1-autoos (bridge session; ListAgents shows its address), server-L1-main, prox-L1-main. Bypass does NOT widen the mandate:
batches/merges only after the router's OK (OS-92 covers router-OK'd batches), record -> ACCEPT -> merge, no push/remote, no browser/suite runs on coding.vm except routing's
D-560 visual-check exception (2 rounds x <= 6 shots, MemAvailable >= 6 GB first, close the browser).

## Where things are
- Repo /home/s/code/plangraph, branch `worktree-proposal` in .claude/worktrees/proposal (main = first commit; K1 + P1 are cherry-picked here, NOT on main; merge to main waits until the UI T3b/G rounds are done, record + routing OK).
- Keeper pilot 127.0.0.1:8787 (config ~/.local/share/plangraph/keeper.json, per-repo refs; DB keeper.db + .pre-* backups; PID file daemon.pid; restart = kill -TERM, never pkill -f). 577 items. Verify: PG_CONFIG=... pg admin verify.
- md-sync (P1) timer + path (~/.config/systemd/user/plangraph-md-sync.*) run tools/md_sync.py with --keeper-config; the gate is LIFTED (routing 04 Oct): the timer applies normally; a MANUAL run needs routing's OK.
- Plan page: user timer plangraph-render.timer (5 min) copies `pg render --cdn` INTO /home/s/code/plangraph/out/plan-cdn.html (same inode, nginx single-file mount); prox's nginx serves it on 192.168.178.73:8089 (firewall: only OPNsense), edge plan.ohje.ooguy.com (Authelia admins/2FA). Since 2026-10-08 (D-639) it shows the NEW UI; the operator still gives the final GRAPH-UI OK.
- Operator preview of the T3b line (fix6) was delivered locally: ~/.local/share/plangraph/preview/plan-t3b-fix6-preview.html (private).
- Job scratch (KEEP): /home/s/.claude/jobs/7caaf7f5/tmp/ui-build/ (L2's clone) and pkg-graphui/ (ALL GRAPH-UI packages, BRIEF-GRAPHUI-{,2,3,4}.md, L1-RULINGS.md with two addenda, HANDOFF-L2-GRAPHUI.md, ACCEPTANCE-GRAPHUI.md). Mine: /home/s/.claude/jobs/fa3662f3/tmp/ (seat records, trials; delete trial DB copies when done).
- Seat records: docs/reviews/2026-10-03-k1-seats.md, 2026-10-04-p1-seats.md. Design note: docs/proposals/2026-10-03-graph-ui-design-note.md.

## Open threads (card has the live detail)
1. GRAPH-UI (D-560, operator TOP priority; operator is the reviewer): accepted heads: fix6 e92c1f2 (+tests ab4abd38); G2b0 3cd43a71; G1 c0711b45+a6c8654+a9563f9a(tests); G2a e6d9236c; G3a 36f5c6a4; G4a 102c6fa7.
   Integrated base gu-integ-ord 3cbfa08 (bundle in pkg-graphui/). In flight at workst: GU-G2b (chip bar + hulls + overview default) steps 3 (fix: strict fake DOM, chip bar placement, zoom clamp, H7 tests, desktop load fits ALL hulls) and
   G2b4 (phone first view: first project header inside, non-focused project leaves hidden). Order after G2b: G3b (focus; single tap TOGGLES, focus = double tap + Focus button), G4b (budget wiring, hover reveal, legend), G5 (phone chips/breadcrumbs, regression A15/A17).
   L2 launches go through routing (`L2-LAUNCH <name> <brief> <cwd>`); the L2 builds each bundle on the previous ACCEPTED head; workst-L1 verifies (real browser, mutations, one non-Google seat) before I accept. Cleanup list (park): dead lines 12-13 of page_graph_view.js, G3a nits, G1 loop-with-lift regression, G2a/G4a test gaps, chipClick prototype-named ids.
2. P1 supersede rule (routing OK, after the GRAPH-UI wiring): a LATER D-row supersedes earlier rows for the same key (reviewed sha -> main sha); only the latest row is attached as evidence; same-row conflicts stay invalid; small agy round + seats; many Server keys wait for it.
3. PROGRESS-PANEL (pg progress + panel in the render; writer package sent, step A taken, step B written by a free model, verification pending at workst), MODEL-KINDS (right after P1/GRAPH-UI wiring), stack design note (Haiku research not started), later M2b off-host keeper.
4. Merge of worktree-proposal to plangraph main (routing OK'd the plan): own record docs/reviews/<date>-main-merge.md; K1-KEEPER close needs `Closes: plangraph:K1-KEEPER@8cdad08` after it.
5. TABLE (operator D-586, priority equal to GRAPH-UI; design note docs/proposals/2026-10-04-table-design-note.md; lanes plangraph:UI-TABLE-MODEL/-VIEW/-WIRE/-PHONE; keeper batch 2026-10-04-router-ui-table-rescope). Packages at workst: TB1 (pure model) PARKED
   (WIP 650dcb5c, page_table_model.js 399 lines, no tests; sandbox + tb1-cont.txt kept), TB2 (DOM view) not launched. Rulings: shared search via PlanUI.Query; phone breakpoint 700 px; windowed rows (1000+); search covers description AND note (router correction) = a small GU-TB1b;
   TB3 is too big (6 files): split TB3a (tab/view switch/boot guard/URL) + TB3b (filter bar/selection->details/__planTable hook), both after G2b accepted. The next L2 round writes TB1b, TB3a, TB3b (briefs: pkg-graphui/BRIEF-TABLE-1.md pattern, L1-RULINGS addenda 3-4).
6. Slot/cost rulings (routing): D-589 open-class Vertex overflow only while the workstation day < $8 (reopens after 00:00Z); D-590 OC-PILOT canary tests moved to an OVH slot, the next free agy slot goes to TABLE; D-592 Server repo ref = origin/improve (done); D-593 OVH Qwen3.8-27B writers allowed for GRAPH-UI step 6 and TB1/TB2 while agy is in quota
   (<= 850k input with the 30-s watcher, <= $2/run, 2 OVH writers per host, seats gpt-oss/Mistral; NOT Coder-30B). Step 6 of GU-G2b runs as gug2b9 (OVH) since 10:34Z at workst. If it fails twice: ask routing for the D-576 escalation (the L2 Sonnet with a browser).
7. Keeper state notes: 14+8+4 missing keys were created from router D-rows (batches 2026-10-04-router-missing-closes-keys-{3,4,5}); O4-BASH-GUARD cancelled as a duplicate of O4-BASHGUARD-PLUGIN (no alias record possible: the alias table is written only by rehome); COST-GATE-CULTURE-FIX has the router's definition.
   The operator preview of the fix6 line is at ~/.local/share/plangraph/preview/plan-t3b-fix6-preview.html (local, plan data).

## Rules learned (do not forget)
- Agy is the preferred writer (D-543: agy first, Vertex medium only if agy fails AND the cap allows, $2/run); max 2 agy writers FLEET-WIDE (3 caused a 3-hour cooldown). Seats non-Google; write the writer family by hand in seat records.
- A real-browser probe is the gate for UI code (a lenient fake DOM hid a strict-mode TypeError). Never accept on green unit tests alone.
- Spawns from a private worktree use a NEUTRAL EMPTY cwd (D-562). Verify a Taildrop file is FILED (ls + sha256) before relaying; files land without the plangraph__ prefix.
- Manual md-sync runs wait for routing's OK. Never run full test suites on coding.vm (D-437): UI files only (about 6 s).
- Seats invent quotes: machine-check every quote; an invented quote invalidates the line. Writers game tests: every task carries the anti-gaming line.
