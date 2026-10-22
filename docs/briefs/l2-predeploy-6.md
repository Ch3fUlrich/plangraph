# L2 brief: build-L2-rtp1-predeploy-6 (successor of predeploy-5 [319b17], gone ~2026-10-19)
Parent: build-L1. Load skill unattended-orchestration. Effort medium. NO crons, no merges/pushes to main, card <= 40 lines, RECYCLE-READY before 230k.
Repo: plangraph (PUBLIC). Keeper claims held by build-L1. Same rules as l2-rt-side-3.md (D-1704, D-2019, D-2038, D-2046/2047, Test-Removal).
Do not bypass the spawner admission cap (now 10, D-2043): queue. No scrub allowlist widening except the judged 2 entries below.
Lanes (merge origin/main in via merge commit; never rebase/squash the listed commits):
1. RT-GATE-WIRING PR #94 (afae587; HIGH): local branch was reset to afae587 (backup-gw-unpushed-d2047 local only). Remaining: judge GO scrub =
   exactly 2 `[[scrub.allow_value]]` entries (rust/worker-core/src/check.rs, redact.rs; value_sha256 of PEM header; why "test-only PEM fixture
   (pem_block), body 64x'A', not a key (mod tests)"); e2e.rs:1496 rename `secret` identifier; fixtures gate_blocks.json -> plain-JSON template
   with synthetic ids + deterministic builder (else drop + list coverage loss). CI green, seats, then MERGE-READY (after #90 merges).
2. RT-P1-OPENCODE-HARDEN PR #95 (c9427a77): synthetic-id fixtures (no allowlist), rename real-429-big-pickle.jsonl, unpushed flatten/rustfmt
   commits via spawner; gemini re-seat (spawner, model agy/gemini-3.8-flash-high pinned) + qwen re-seat on final head.
3. RT-P1-JEV-SHADOW PR #96 (2a8bb220): final-head seats, timeout cap 10 s, remove leftover QC axis.
4. RT-P1-WF-VERSIONING (unpushed driver work, leg 7): push with run-id trailers, PR.
5. PHASE-RUNNER after GATE-WIRING done (build-L1 claims).
Report to build-L1 only: MERGE-READY / BLOCKED lines as in rt-side-3 brief.
