# build-L2-card-checks brief (build-L1, 2026-10-22, D-2201)
Parent: build-L1 (coding.vm; reply via the cross-session channel or workst-L1-autoos relay). Load skill unattended-orchestration. Sonnet, effort medium, slim MCP.
Repo: plangraph (PUBLIC: never site strings, seat prompts or private audit text in commits/PRs/comments). Keeper claims held by build-L1: do NOT claim/submit/verdict.
Own git worktree off origin/main (merge origin/main in via merge commit; never rebase/squash existing commits). NO push/merge to main, NO crons, NO live steps.
A permission/classifier denial stops that step: send the verbatim refusal to build-L1, never work around it.
FILES (D-2198): every writer brief names the exact FILES it may touch (paths, new/edit), tests + CHANGELOG included; >3 code files = split writers.
WRITERS (D-2148, D-2046/2047): non-Claude only, via the autoos-agent spawner (D-2038). cx/gpt-6.1-sol first (served on the workstation gateway) > mimo/mimo-v2.6-flash > omniroute/deepseek-direct-flash; record each skip + reason. NEVER Sonnet/Claude writers. Every new commit carries `Writer: <served model> <run id>`; removing a test needs `Test-Removal: D-NNNN`.
SEATS (D-2195/D-2197): non-Claude, family != writer and != each other: opencode free > openrouter free (laguna-s-2.1, north-mini-code; no nemotron) or deepseek-v4-flash while OR free fails > qodercli qwen3.8-flash > agy gemini-3.8-flash > mimo/mimo-v2.6-flash > agy claude-sonnet-5-5-medium (never the non-Claude seat of a HIGH lane). Seat record = served model + provider + family + verdict.
D-2202: mimo only by provider-prefixed id mimo/mimo-v2.6-flash, never a combo; a local 502 "Cannot clone object" = combo bug -> retry with the provider-prefixed id.
Heavy suites: 1 per orchestrator, 2 per host. Before MERGE-READY: pytest + pytest-postgres + scrub + rust (if rust touched) green on the exact head (gh workflow run tests.yml --ref <branch>).
Report to build-L1 ONLY: MERGE-READY <PR> <sha> <seats>, BLOCKED <item> | what | options *default, GO-REQ (HIGH). No acks/ALIVE. Card <= 40 lines; RECYCLE-READY <card> before 230k.

# L2 brief: build-L2-card-checks   (lane C)
## Lane: plangraph:RT-P1-CARD-CHECKS (standard: 2 non-Claude seats, family != writer)
No branch yet: create lane/rt-p1-card-checks off origin/main. Read docs/p1-audit is PRIVATE (not in this repo): work from the item text below + code anchors
(readiness.py:222-240, tools/seat_verdict.py:137-141, rust/worker*, gate activity). Note: #94 RT-GATE-WIRING (gate as named checks) is being finished in parallel by build-L2-spine;
the diff-scan check goes into the gate check list it introduces: fetch lane/rt-gate-wiring-r and build on its check API (merge it in once #94 is on main); if it is not merged when you
need it, keep your gate change in a separate commit so it rebases cleanly by merge. Split writers per file group (worker pre-attempt, forbid paths, gate diff scan, held-out/smoke/exports).

## Keeper item text (read-only copy)
plangraph:RT-P1-CARD-CHECKS | Card check quality: red-first check, protected check files, test-gaming diff scan
state claimed | kind lane | track RETHINK | prio 7 | epoch 2
claimed_by build-L1 | delegate - | lease 2026-10-22T16:38:08Z | reviewer -
deps plangraph:RT-P1-PLANNER-KIT=done
unblocks plangraph:PH-RT-RUNTIME, plangraph:RT-P1-E2E
desc Audit G6 (D-1998), research docs/p1-audit/research/c-cards-contracts.md and b-gates-merge.md: readiness only dry-runs check_cmd (readiness.py:222-240), held-out is optional, smoke_checks and exports are never run, test gaming is judged only by the reviewer self-report (seat_verdict.py:137-141). Design D8 asks for a real deterministic check per unattended card.
done Worker pre-attempt runs the check on the base sha in the card worktree: if it already passes, a change card parks unless check_kind=regression; files the check reads are forbid paths for the writer unless listed in the write set; gate activity scans the diff for deleted/skipped tests and removed asserts (finding -> check_fail with reason); held-out check required for risk-tagged cards; smoke_checks run; exports signatures verified after the attempt; tests; 2 seats of non-Claude families different from the writer (D-2148 order).

