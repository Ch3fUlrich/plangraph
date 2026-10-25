# build-L1 card · gen-33 · 2026-10-25 (D-2812 relaunch) · NO cron · hb loop = jobs/0bbdff0b/tmp/hb.sh (restart on exit)
D-2739 (operator): one task loops (no follow-up items), design first for HIGH/>400 lines, slices <=400, 2 text seats, MED in body=changes, fixer>=writer never flash, round>3 -> Opus replan, merge main once before final review.
D-2749: AutoOS spawner retired; all runs via plangraph spawner; missing feature -> BLOCKED to routing, no fallback.
D-2744: no deepseek leg (writer or seat, incl opencode free deepseek); mimo-pro, mimo-flash seats only.
D-2754: design/replan by me or in-process Opus Agent; code writers+seats via plangraph spawner, pin --leg mimo-v2.6-pro (never deepseek); in place on coding.vm OK for opencode/mimo/qodercli w/ .git/config diff check, no agy in place.
Rules: D-2643/2644 batch review at end; D-2534 phase-batch; D-2539 Haiku first w/o Claude writer; D-2540 first review 2 families, re-review 1; D-2511 seats != any writer family.
Writers D-2683/2684: agy gemini > mimo-pro > OR free/opencode free (~01Z) > mimo-flash; never Claude; cx out ~10-30. agy -p never a seat (L24). D-2706: floor MemAvailable 2000 MB (incl. projected RSS), max 4 writers here, rest -> workst-L1-autoos. One compaction = recycle (new session from handoff).
Router 10:3xZ: D-2706 floor 2000 MB incl projected RSS REPLACES D-2686 4000.
D-2716: no new writers on coding.vm until <=4 agent-run procs; route to workst.
Spawn (D-2749/D-2795): jobs/0bbdff0b/tmp/gspawn2.sh <wt> <brief> <tag> <reason> <leg> (host gate D-2823 pgrep opencode|qodercli|agy runs<4 + MemAvailable>=2000 IN the spawn cmd; my budget max 2 concurrent, D-2800). PYTHONPATH=spawner-host-keys/src. Legs: mimo-v2.6-pro, agy-gemini-3.8-flash. Fill Writer RUNID via filter-branch before push.
Handoff = status/build-L1.handoff.md. Control repo local only: git push control build-L1.
| item | state / next |
|---|---|
| SPAWN-WIN-PARITY (HIGH, claimed) | S1 PR #210 head 0d3a7d99 CI 40212967356 red (8 tests: no URL in hermetic tests -> I6 refusal; prod OK). qwen pass, Haiku 5 MED. Fix r1 mimo-pro gspawn2 tag swp-s1f1 (brief-swp-s1-fix1.md) -> RUNID fill, push, CI, delta qwen+Haiku -> judge GO per sha |
| KEEPER-PG-RECONNECT (HIGH) | S1 #206 MERGED 8d0104dd (D-2826, code only). Next S2: brief $J/brief-pgr-s2.md (agy), new wt from main; 2 seats + judge GO |
| REVIEW-METHOD-SETTING #209 | MERGED da92ca78 (D-2824). in_review until #211 merges -> post seat-rms-f2b-haiku unchanged |
| CI-SELECT-DEPLOY-REFS #208 | MERGED f413f175. in_review; seat-cisel3-haiku is fix-MED (refuted) -> ask routing which verdict to post |
| spawn-iso (#200/#201, held from dead L2) | stalled run-feb506c2 cancelled. Re-spawn after pgr-fix2 (order S1 > pgr > iso > shm-leak) |
| PG-TEST-SHM-LEAK (std, prio 1) | defined D-2800; non-Claude writer, ~30 lines: pid-named /dev/shm dir + dead-pid sweep in tests/conftest.py |
| SPAWN-OPENROUTER-FREE-ENDPOINTS (std, prio 5) | defined D-2805; after shm-leak; account/privacy change = operator OS line |
| SQ12 (workst-L1-autoos) | slices SQ12-a/-b; workst writes held until SPAWN-WIN-PARITY lands |
| efficiency slice 4 | sent host-wide admission gap (D-2785) + qwen/haiku review legs (D-2789). D-2799 4c argv OK sent gen-33 w/ 5 conditions (accepted); slice 4 on hold for routing (design r4 FIX) |
| D-2810/D-2815 | defined: SPAWN-PACKET-SCRUB (HIGH prio 3, strong design first, 2 non-Claude seats + judge), SPAWN-HAIKU-LEG (std prio 4). Order S1 > pgr > iso > shm-leak > PACKET-SCRUB > or-free > HAIKU-LEG, budget 2 |
| coding.vm RAM | 10-25 cleanup: 72 stale /dev/shm/pgtest-* (4.7G) + orphan opencode serve removed; avail 1.8->8.2G |
Seats: seatpost.py/qwenprompt.py/haikuseat.sh interim OK (D-2789): count toward cap, never edit review.txt/prompt.txt, keep seat dirs; post via post.sh (verdict.judge -> tools/seat_verdict.py reviewer-<family> token).
Traps: zsh -> bash -c; mergepath needs claim; never dump environ; Hand-Edit <=10 lines (gate); define new lane = JSON to router (D-2230).
| REVIEW-VERDICT-PARSE #211 (std, claimed) | head 447ec5b5; qwen found **MED** false pass -> fix r1 rvpf1 mimo-pro (brief-rvp-fix1.md) -> qwen delta -> merge -> post 2 verdicts (D-2825) |
