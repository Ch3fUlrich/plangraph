# L2 brief: build-L2-oc-harden   (lane E)
## Lanes, in order:
1. plangraph:RT-P1-OPENCODE-HARDEN PR #95 (lane/rt-p1-opencode-harden, head c9427a77): synthetic-id fixtures (no scrub allowlist widening), rename real-429-big-pickle.jsonl,
   the unpushed flatten/rustfmt commits from the predecessor build-L2-rtp1-predeploy-5/-6 worktree on this workstation (ask workst-L1-autoos for the path; reuse only commits with
   non-Claude Writer trailers, else redo via a writer); re-seat on the final head (2 families != writer). HIGH (harness env + auth files): + judge GO-REQ via build-L1.
2. plangraph:RT-P1-JEV-SHADOW PR #96 (lane/rt-p1-jev-shadow, head 2a8bb220): final-head seats, timeout cap 10 s, remove leftover QC axis. Done needs judge too.

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

