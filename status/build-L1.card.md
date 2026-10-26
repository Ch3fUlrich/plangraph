# build-L1 card · gen-36 -> 37 handoff · 2026-10-26 03:5xZ · NO cron · hb loop = jobs/0bbdff0b/tmp/hb.sh 90 · handoff status/handoff-build-L1.md
D-2739 (operator): one task loops (no follow-up items), design first for HIGH/>400 lines, slices <=400, 2 text seats, MED in body=changes, fixer>=writer never flash, round>3 -> Opus replan, merge main once before final review.
D-2749: AutoOS spawner retired; all runs via plangraph spawner; missing feature -> BLOCKED to routing, no fallback.
D-2744: no deepseek leg (writer or seat, incl opencode free deepseek); mimo-pro, mimo-flash seats only.
D-2754: design/replan by me or in-process Opus Agent; code writers+seats via plangraph spawner, pin --leg mimo-v2.6-pro (never deepseek); in place on coding.vm OK for opencode/mimo/qodercli w/ .git/config diff check, no agy in place.
Rules: D-2643/2644 batch review at end; D-2534 phase-batch; D-2539 Haiku first w/o Claude writer; D-2540 first review 2 families, re-review 1; D-2511 seats != any writer family.
Writers D-2683/2684: agy gemini > mimo-pro > OR free/opencode free (~01Z) > mimo-flash; never Claude; cx out ~10-30. agy -p never a seat (L24). D-2706: floor MemAvailable 2000 MB (incl. projected RSS), max 4 writers here, rest -> workst-L1-autoos. One compaction = recycle (new session from handoff).
Router 10:3xZ: D-2706 floor 2000 MB incl projected RSS REPLACES D-2686 4000.
D-2716: no new writers on coding.vm until <=4 agent-run procs; route to workst.
Spawn (D-2749/D-2795; D-2892 spawner code = detached ~/code/plangraph-worktrees/spawner-main, gspawn2 refreshes it to origin/main): jobs/0bbdff0b/tmp/gspawn2.sh <wt> <brief> <tag> <reason> <leg> (host gate D-2823 pgrep opencode|qodercli|agy runs<4 + MemAvailable>=2000 IN the spawn cmd; my budget max 3 concurrent, D-2840). PYTHONPATH=spawner-main/src. Legs: mimo-v2.6-pro, agy-gemini-3.8-flash. Fill Writer RUNID via filter-branch before push.
Handoff = status/build-L1.handoff.md. Control repo local only: git push control build-L1.
| item | state / next |
|---|---|
| SPAWN-WIN-PARITY (HIGH, claimed) | S1 #216/#217/#218 MERGED (main 2bdde74a, evidence ok). S3 #213 head 338b4f6a (r2 mimo-pro + L1 1-line stub Hand-Edit); CI 40251267395 green; delta Haiku fix refuted ($J/seat-s3d2-haiku/refutation-L1.txt); MERGED d66bf9e5 (D-2879, CI 40251787608, evidence ok); told workst (mimo-leg test, release 4 Server lanes + AO seat) + server-L1-main [c46235]. S2 #219 MERGED 7b388a51 (GO D-2885); finish219.sh -> evidence. S2 LOWs (I12 fallthrough test, _comm TypeError fallback, payload cleanup on factory exc) -> fix log. workst GREEN mimo-leg run-f5ad2f0f |
| KEEPER-PG-RECONNECT (HIGH) | S1 #206 MERGED 8d0104dd, evidence commit+ci attached (main CI 40216891777). S2 wt ~/code/plangraph-worktrees/keeper-pg-reconnect-s2 @8d0104dd, brief $J/brief-pgr-s2.md (agy); S2 #220 MERGED f50982d6 (GO D-2889, code only); finish220.sh -> evidence. S3 next (design WRITER BRIEF S3, mimo-pro/agy; D-2888 limits). Live redeploy = own GO + OS-KEEPER-LS9-MIGRATE; 2 seats + judge GO; live redeploy = own GO |
| REVIEW-METHOD-SETTING #209 | MERGED da92ca78. glm re-run (D-2898) run-555b136f pass, TEST-GAMING ok, served_model still unwitnessed on main -> NOT posted, stays in_review; defects (a)(b) in REVIEW-VERDICT-PARSE fix log (notes) |
| CI-SELECT-DEPLOY-REFS #208 | MERGED. D-2835: no more seats/decisions; closes when ORCH-EFFICIENCY-ENFORCE pt5 posts refuted seats (r1 cisel4 + refutation-D-2833.txt) |
| spawn-iso #200 / #201 | #200 DONE 2348abf2. #201 MERGED 2fe388af; finish201.sh -> evidence/submit/post (check $J/finish201.log) |
| PG-TEST-SHM-LEAK (std, claimed) | PR #222 head e0748276 (agy run-ed01799d, L1 re-run 8 passed, gate 0); qwen seat shm running -> merge -> evidence |
| SPAWN-OPENROUTER-FREE-ENDPOINTS (std, prio 5) | defined D-2805; after shm-leak; account/privacy change = operator OS line |
| SQ12 (workst-L1-autoos) | slices SQ12-a/-b; workst writes held until SPAWN-WIN-PARITY lands |
| efficiency slice 4 | sent host-wide admission gap (D-2785) + qwen/haiku review legs (D-2789). D-2799 4c argv OK sent gen-33 w/ 5 conditions (accepted); slice 4 on hold for routing (design r4 FIX) |
| D-2810/D-2815 | defined: SPAWN-PACKET-SCRUB (HIGH prio 3, strong design first, 2 non-Claude seats + judge), SPAWN-HAIKU-LEG (std prio 4). Order S1 > pgr > iso > shm-leak > PACKET-SCRUB > or-free > HAIKU-LEG, budget 2 |
| coding.vm RAM | 10-25 cleanup: 72 stale /dev/shm/pgtest-* (4.7G) + orphan opencode serve removed; avail 1.8->8.2G |
Seats: seatpost.py/qwenprompt.py/haikuseat.sh interim OK (D-2789): count toward cap, never edit review.txt/prompt.txt, keep seat dirs; post via post.sh (verdict.judge -> tools/seat_verdict.py reviewer-<family> token).
Briefs: append status/writer-brief-tail.md (D-2885 no writer Test-Removal/Gate-Edit). Merges: pass commit_message="Plan-Item: <KEY>".
Traps: zsh -> bash -c; mergepath needs claim; never dump environ; Hand-Edit <=50 lines (D-2853); gate check needs --base/--head; define new lane = JSON to router (D-2230).
| REVIEW-VERDICT-PARSE loop (D-2898/D-2900, PRIO 1) | (b) opencode served_model witness via opencode.db session title + (a) TEST-GAMING regex; wt ~/code/plangraph-worktrees/opencode-witness lane/opencode-served-witness; mimo-pro ocw1 ($J/brief-ocw.md) running -> re-run tests -> PR -> seats (not xiaomi/anthropic) -> re-run RMS glm post |
| ORCH-EFFICIENCY-ENFORCE (HIGH prio 10, claimed epoch 3) | #215 MERGED 43aebc1d (GO D-2861), evidence ok (main CI 40235422603); efficiency told, E1a rebased 94e5f316. D-2879 gate/git trailer mismatch -> E1a buildl1-T1 (efficiency owns). Keeper seats post at submit (Haiku same_family via fe7c4926 -> need glm after reset). #214 MERGED 7bbd11a6 |
