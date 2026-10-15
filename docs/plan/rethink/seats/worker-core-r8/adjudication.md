# worker-core r8 seat (ovh/gpt-oss-120b, openai) on PR #6 head 291617ff — build-L1 gen-3, 2026-10-15
Prompt: r6 vector head (V1-V6) + delta 7e5a61e3..291617ff + ops/tail.md. finish=stop, VERDICT pass, TEST-GAMING no.
Spot-check: git.rs check_rev rejects empty/leading '-'/whitespace/control (l.24-32) as the seat states; cfg!(test) `hermetic`
only sets git env (no system/global config, fixed identity) = test hermeticity, not behaviour gaming. Accepted as recordable.
Deferred to RT-P1-WORKER: symlink escape sandbox, 24 h wait cap -> park, probe_fail attempt cap.
