# build-L2-oc-harden brief (build-L1, 2026-10-22, D-2201)
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

# L2 brief: build-L2-oc-harden   (lane E)
## Lanes, in order:
1. plangraph:RT-P1-OPENCODE-HARDEN PR #95 (lane/rt-p1-opencode-harden, head c9427a77): synthetic-id fixtures (no scrub allowlist widening), rename real-429-big-pickle.jsonl,
   the unpushed flatten/rustfmt commits from the predecessor build-L2-rtp1-predeploy-5/-6 worktree on this workstation (ask workst-L1-autoos for the path; reuse only commits with
   non-Claude Writer trailers, else redo via a writer); re-seat on the final head (2 families != writer). Standard tier.
2. plangraph:RT-P1-JEV-SHADOW PR #96 (lane/rt-p1-jev-shadow, head 2a8bb220): final-head seats, timeout cap 10 s, remove leftover QC axis. Standard tier.

## Keeper item text (read-only copy)
plangraph:RT-P1-OPENCODE-HARDEN | OpenCode harness hardening for dogfooding: pinned version, JSON events, empty-run detection, permission config
state claimed | kind lane | track RETHINK | prio 8 | epoch 2
claimed_by build-L1 | delegate - | lease 2026-10-22T16:37:53Z | reviewer -
deps -
unblocks plangraph:PH-RT-RUNTIME, plangraph:RT-P1-E2E, plangraph:RT-P1-FAILURE-DRILLS, plangraph:TOKEN-AB-SESSION, plangraph:WORKER-TOOL-SURFACE
desc Audit G5 (D-1998), research docs/p1-audit/research/d-opencode-harness.md: exit status on provider errors is undocumented and exit_class reads stderr only on nonzero exit; --attach not in the 2.0.16 help on coding.vm; no permission config; version pin opt-in (harness.rs PG_HARNESS_VERSIONS). Pulls the opencode token part of RT-HARNESS-USAGE forward. HIGH: harness env and auth files.
done Worker refuses an opencode binary not matching the pinned version; run uses --format json and classifies provider error, 429 and empty output from events (never ok); a no-diff attempt is exit class no_change; per-attempt permission config denies paths outside the worktree and never asks; tokens in/out from events reach the attempt row; recorded real 429 + empty-run fixtures; 2 non-Claude seats.

plangraph:RT-P1-JEV-SHADOW | Jev decision model in shadow on routing and quality-accept decisions (design D18)
state claimed | kind lane | track RETHINK | prio 6 | epoch 2
claimed_by build-L1 | delegate - | lease 2026-10-22T16:37:53Z | reviewer -
deps -
unblocks plangraph:JEV-READINESS-SHADOW-QS, plangraph:PH-RT-RUNTIME
desc Operator D-1981: integrate Jev now. Design D18 + section 3 step 3: rules decide, free Jev answers the same single-axis questions in shadow; rules answer, Jev answer and outcome are logged. Extends to the QC accept decisions (qc-checks.md Q1-Q5). RT-P5-JEV stays the later adopt/reject evaluation on these logs.
done for every routing decision (harness, window class, attempt budget, review level) and every QC accept decision, a free Jev leg answers the same question in shadow; rule answer + Jev answer + outcome stored per card/attempt in the keeper and shown by show/API; Jev never changes a decision; agreement rate report; timeout/unavailable Jev never blocks; tests; 2 non-Claude seats + judge.

