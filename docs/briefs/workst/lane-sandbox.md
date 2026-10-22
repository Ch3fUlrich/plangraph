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

## Keeper item text (read-only copy)
plangraph:RT-P1-ATTEMPT-SANDBOX | OS confinement for Rust worker attempts: FS write jail, per-attempt cgroup limits, WSL drive/interop off, no attach leg unattended
state claimed | kind lane | track RETHINK | prio 8 | epoch 1
claimed_by build-L1 | delegate - | lease 2026-10-22T16:37:58Z | reviewer -
deps plangraph:RT-P1-WORKTREE-ROOT-DRILL=done
unblocks plangraph:PH-RT-RUNTIME, plangraph:RT-P1-E2E
desc S5 REPORT section 5 gaps H1, H3-H5, H12. Today: confine.rs only checks the diff after the attempt (rust/worker-core/src/confine.rs), sandbox_env.rs scrubs env + private HOME, stall.rs kills the process group; zero hits for bwrap/unshare/cgroup/landlock/systemd-run in rust/worker*. bash-guard is not a control here: the Rust worker does not load it and it fails open on missing script, timeout, spawn error or non-zero exit (AutoOS configuration/opencode/plugins/bash-guard/index.mjs:1550-1585). Steps: (1) apply the WORKTREE-ROOT-DRILL outcome (isolate clone if reproduced); (2) FS jail: only worktree + private HOME + tmp writable; (3) systemd-run --user --scope with MemoryMax/TasksMax per attempt; (4) worker WSL distro automount and interop off, or the jail hides /mnt; (5) refuse the OpencodeServer --attach leg for unattended cards (harness.rs); (6) optional egress limited to gateway + git remote. Scope split with plangraph:AGENT-JAIL: AGENT-JAIL = READ-deny for spawner legs (opencode/qoder/agy seats and writers started by the autoos-agent spawner, owner workst-L1-autoos); this item = WRITE + resource confinement of Rust worker attempts (owner build-L1). Neither depends on the other; whichever lands first ships the jail helper (bwrap/unshare wrapper) and the second reuses it; no second jail implementation.
done A test attempt writing outside its worktree (absolute, relative ../, /mnt/c, HOME) fails at OS level; a fork bomb and a memory hog are stopped by the scope limits; the attach leg is refused for unattended cards (test); the drill outcome applied (isolate clone if reproduced); jail helper shared with AGENT-JAIL (one implementation); 2 seats of non-Claude families different from the writer (D-2148 order) plus judge.

