# Shared push targets and abandoned private targets (D-6054)

Preflight replaces private Cargo targets with the host shared target and holds
the existing cleanup lease, checking the shared cap before and after Cargo.
Native Codex children explicitly inherit the same target with incremental builds
disabled. Writer and reviewer briefs forbid private targets.

Disk hygiene reclaims Cargo-marked private targets idle for over 30 minutes in
the first two levels of /tmp and ignored, untracked rust/target directories in
registered worktrees under its configured root. Shared roots, protection records,
processes, leases, ownership and symlink checks prevent unsafe reclaim.
The full Rust gate exposed a Git fixture inheritance race; the existing fixture
helper explicitly suppresses Git config injection even when absent at configure.
The running-cancellation fixture now waits for launch instead of a fixed delay;
its original five-second deadline and all status/PID assertions remain intact.

This is the first slice under the 600-line limit. Open: exact-head fast-check
stamps and build reuse; continuous cap enforcement for standalone preflight and
native Codex lifecycles (preflight checks the cap at command boundaries).
