# build-L1 card · gen-32 · 2026-10-25 (D-2767 relaunch) · NO cron · hb loop = jobs/0bbdff0b/tmp/hb.sh (restart on exit)
D-2739 (operator): one task loops (no follow-up items), design first for HIGH/>400 lines, slices <=400, 2 text seats, MED in body=changes, fixer>=writer never flash, round>3 -> Opus replan, merge main once before final review.
D-2749: AutoOS spawner retired; all runs via plangraph spawner; missing feature -> BLOCKED to routing, no fallback.
D-2744: no deepseek leg (writer or seat, incl opencode free deepseek); mimo-pro, mimo-flash seats only.
D-2754: design/replan by me or in-process Opus Agent; code writers+seats via plangraph spawner, pin --leg mimo-v2.6-pro (never deepseek); in place on coding.vm OK for opencode/mimo/qodercli w/ .git/config diff check, no agy in place.
Rules: D-2643/2644 batch review at end; D-2534 phase-batch; D-2539 Haiku first w/o Claude writer; D-2540 first review 2 families, re-review 1; D-2511 seats != any writer family.
Writers D-2683/2684: agy gemini > mimo-pro > OR free/opencode free (~01Z) > mimo-flash; never Claude; cx out ~10-30. agy -p never a seat (L24). D-2706: floor MemAvailable 2000 MB (incl. projected RSS), max 4 writers here, rest -> workst-L1-autoos. One compaction = recycle (new session from handoff).
Router 10:3xZ: D-2706 floor 2000 MB incl projected RSS REPLACES D-2686 4000.
D-2716: no new writers on coding.vm until <=4 agent-run procs; route to workst.
Spawn (D-2749/D-2795): jobs/0bbdff0b/tmp/gspawn2.sh <wt> <brief> <tag> <reason> <leg> (host gate pgrep runs<4 + MemAvailable>=2500 IN the spawn cmd; my budget max 2 concurrent, D-2800). PYTHONPATH=spawner-host-keys/src. Legs: mimo-v2.6-pro, agy-gemini-3.8-flash. Fill Writer RUNID via filter-branch before push.
Handoff = status/build-L1.handoff.md. Control repo local only: git push control build-L1.
| item | state / next |
|---|---|
| SPAWN-WIN-PARITY (HIGH, claimed) | design briefs/lanes-d2657/spawn-win-parity-design.md (§7 answers, §8 WITHDRAWN, §9 D-2809 in force: rendered `mimo` provider, gateway baseURL, env key; no omniroute/mimo). S1 agy writer swp-s1c (continues WIP ae0091fe). Then seats qwen+Haiku, judge GO per sha; S2/S3 next. Smoke incl agy+qodercli init on workst (D-2804) |
| KEEPER-PG-RECONNECT #206 (HIGH) | S1 head 7e34a26a; fix2 brief brief-pgr-s1-fix2.md (Haiku MED _sent keep-alive real). Re-spawn pgr-fix2b running. Then delta seats, GO-REQ; S2/S3 after |
| REVIEW-METHOD-SETTING #207 | MERGED 8b6fba60, CI green. Keeper in_review, alibaba pass only (409 same_principal); asked router for 2nd family D-id |
| CI-SELECT-DEPLOY-REFS | #196 merged; fix1 PR #208 8723236e. qwen delta said fix (fallback test) - refuted: test exists l.141. Re-seat with full test file (material cisel-fix1b-material.txt) when budget allows |
| spawn-iso (#200/#201, held from dead L2) | stalled run-feb506c2 cancelled. Re-spawn after pgr-fix2 (order S1 > pgr > iso > shm-leak) |
| PG-TEST-SHM-LEAK (std, prio 1) | defined D-2800; non-Claude writer, ~30 lines: pid-named /dev/shm dir + dead-pid sweep in tests/conftest.py |
| SPAWN-OPENROUTER-FREE-ENDPOINTS (std, prio 5) | defined D-2805; after shm-leak; account/privacy change = operator OS line |
| SQ12 (workst-L1-autoos) | slices SQ12-a/-b; workst writes held until SPAWN-WIN-PARITY lands |
| efficiency slice 4 | sent host-wide admission gap (D-2785) + qwen/haiku review legs (D-2789). D-2799: review 4c scope argv design 742727c8 after 4a/4b |
| D-2810 | 4 edges removed (HAIKU-PROPAGATE, LESSONS-ENFORCE, PH-RT-RUNTIME, RT-P4-SKILLS-REWRITE -> AO-*): on touch check if a plangraph-spawner item is needed, JSON to router |
| coding.vm RAM | 10-25 cleanup: 72 stale /dev/shm/pgtest-* (4.7G) + orphan opencode serve removed; avail 1.8->8.2G |
Seats: seatpost.py/qwenprompt.py/haikuseat.sh interim OK (D-2789): count toward cap, never edit review.txt/prompt.txt, keep seat dirs; post via post.sh (verdict.judge -> tools/seat_verdict.py reviewer-<family> token).
Traps: zsh -> bash -c; mergepath needs claim; never dump environ; Hand-Edit <=10 lines (gate); define new lane = JSON to router (D-2230).
