# WORKER-CORE round 2 adjudication (build-L1, 2026-10-15) — lane/rt-p1-worker-core-r2 240dd4dc (+291617ff test-helper only)
r6 (ovh gpt-oss-120b): pass, V1-V6 answered; test_gaming=missing (line ended "found none", not ": 0 hits") -> not recordable.
r7 (same model, same prompt + stricter tail): fix, re-asserting the 3 round-1 HIGHs. Checked against code, all three claims are factually wrong:
1. "changed_files uses --diff-filter=ACMRT, drops the delete side of a rename": no --diff-filter anywhere; confine.rs:84-86 diff --name-only --no-renames (both paths); test confine.rs:195 rename_reports_both_paths.
2. "promotion on a dirty worktree, budget.rs:next_action": file is budget/mod.rs; next_action is pure policy (no I/O by design, card says "as pure functions"). Design 4.5 l.126 "promotion ... from a clean worktree" = the glue creates a fresh worktree for the promoted attempt -> carried as a requirement into RT-P1-WORKER.
3. "core never emits Quarantine for flaky": budget/mod.rs:128 `ExitClass::FlakyCheck => return Action::Quarantine`, matching design l.128 "flaky_check -> quarantine the card to the Inbox".
Ruling: r7 rejected (hallucinated evidence). Need one clean recordable seat; next = ovh qwen3.8-27b (alibaba, D-1329).
Carry to RT-P1-WORKER: fresh worktree on promotion; symlink escape sandbox; 24 h wait cap -> park; probe_fail attempt cap.
