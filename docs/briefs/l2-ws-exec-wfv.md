# build-L2-exec-wfv brief (build-L1, 2026-10-22, D-2201)
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

# L2 brief: build-L2-exec-wfv   (lane D)
## Lanes, in order: plangraph:RT-P1-EXEC-OWNERSHIP, then plangraph:RT-P1-WF-VERSIONING (keeper verbs + Temporal = HIGH: 2 seats + judge GO-REQ)
State: neither has a pushed branch. Unpushed driver work for WF-VERSIONING (leg 7) and any EXEC work may exist in the workstation worktree of the predecessor
L2 build-L2-rtp1-predeploy-5 / -6 (ask workst-L1-autoos for its path). Inspect it read-only: if commits there carry valid Writer trailers + pass scrub, push them to
lane/rt-p1-wf-versioning / lane/rt-p1-exec-ownership; commits without a non-Claude Writer trailer are NOT reused (D-2046): rewrite via a new writer run.

## Keeper item text (read-only copy)
plangraph:RT-P1-EXEC-OWNERSHIP | One owner per fact for card execution: worker verbs fenced by execution identity, forward-only card states, reaper leaves Temporal states alone
state claimed | kind lane | track RETHINK | prio 8 | epoch 2
claimed_by build-L1 | delegate - | lease 2026-10-22T16:37:53Z | reviewer -
deps plangraph:RT-P1-VERBS-LIVE-GUARDS=done
unblocks plangraph:PH-RT-RUNTIME, plangraph:RT-P1-E2E
desc Audit gemini R3/R4 (D-1998): the worker sets item state directly (store_worker.py:96-110 _worker_park -> _apply_state parked) beside the projector; _apply_state has no forward-only table (store_cards.py:33,397); the worker never claims cards (claim_next only in tests) while LIVE-GUARDS G1 fences park by claim epoch/lease, and store_reap.py resets lapsed claims every 60 s. Design 2: Temporal owns every clock, one projector writes. HIGH: keeper write path.
done Worker verbs (park, complete, ask_question, alarms) carry (workflow_id, run_id, attempt); keeper refuses a call whose run_id/attempt is older than the recorded one; card state changes only via the projector or a forward-only transition table (back moves need a new run_id); reaper never touches running/waiting/needs_input cards; test: a delayed worker retry cannot overwrite a newer projector state; SQLite + PG; 2 non-Claude seats.

plangraph:RT-P1-WF-VERSIONING | Workflow versioning + durability as general rules: versioning on, frozen replay fixtures, patch-or-drain, real-restart test
state claimed | kind lane | track RETHINK | prio 8 | epoch 2
claimed_by build-L1 | delegate - | lease 2026-10-22T16:37:54Z | reviewer -
deps plangraph:RT-P1-WORKER=done
unblocks plangraph:PG-APPROVED-FIXTURES, plangraph:PH-RT-RUNTIME, plangraph:RT-P1-E2E, plangraph:RT-P1-FAILURE-DRILLS
desc Audit G4 (D-1998) + judge #83 conditions (drain/replay changed park args; real shutdown restart). Today versioning is opt-in (rust/worker/src/buildid.rs), replay fixtures are re-recorded in place (4d4b02ad), no patched() markers, no Continue-As-New. Makes the lane-level judge conditions a rule for every workflow change.
done Worker refuses to start without deployment + build id; start script sets the new build current (no stalled queue); replay fixtures append-only per released build, CI fails if one changes; workflow-change PR check: patch marker or new version + drain note; CI SIGKILLs the worker mid-attempt and mid-timer, a new build resumes, workflow completes; schedule-to-close on activities; Continue-As-New above an event/wait cap; runbook; 2 non-Claude seats.

