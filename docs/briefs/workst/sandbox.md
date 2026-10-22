# L2 brief: build-L2-sandbox   (lane A)
## Lane: plangraph:RT-P1-ATTEMPT-SANDBOX (HIGH: 2 non-Claude seats + judge GO via build-L1)
PR #100 draft, branch spine/attempt-sandbox, head cd0736767af6d92d7b3563a882e438ddd8a036c2; CI all green there. Previous seats (qwen fix, glm fix, OR-free fail/no family) found:
M1 /proc ro lets the child read same-uid /proc/<pid>/environ; M2 shared tmp not per-attempt; M3 no Landlock REFER / ABI>=V2 (EXDEV across rw roots); M4 PG_JAIL_BIN not canonicalized/absolute; L from_env unjailed outside the worker bin (documented).
build-L1 decision: fix M2 (per-attempt tmp), M3 (require ABI>=V2 + REFER, fail closed below), M4 (canonical absolute PG_JAIL_BIN, refuse otherwise) via writer.
M1: in the child pre_exec allow ro only /proc/<own pid> (covers /proc/self) + /proc/{cpuinfo,meminfo,stat,loadavg}; tests: node starts; /proc/1/environ and a sibling same-uid pid environ refused. If node breaks, fall back to documented limit and say so in the PR body. L stays documented.
Worker now fails closed without PG_JAIL_BIN or PG_JAIL=off: add PG_JAIL_BIN (PG_JAIL=off only in the devstack test profile, never default) to the devstack e2e config in this PR.
Known small manual fixes after writer output: 1 call site, clippy char pattern, rustfmt (writer commit, not you).
Done criteria (keeper): see below. Then re-seat on the final head, MERGE-READY.
Also fix (glm seat extras): hog/fork tests assert only !success (assert the limit kill reason); deny_read test never passes --deny-read; set_env in a parallel test (race); dead jail_exec; unused libc dep in pg-jail.
Prior writers: mimo-v2.6-flash + deepseek-flash (tool-less) -> seats must not be xiaomi/deepseek family unless that writer's code is not under review; safest: qwen + google/meta/zhipu.
Briefs/seat outputs of the previous run live on coding.vm only (not readable here); everything needed is in this brief. pg-worker crate needs protoc (CI has it).
