# CODEX-HOST-READINESS

Add `plangraph codex-host-readiness WORKTREE SANDBOX_MODE`, a Linux, model-free
pre-package check for Codex installation/login, persisted folder/hook trust,
startup update prompts, sandbox launch, scratch linked-worktree commits and RAM.
Failures block package admission with one line per missing point; prepared hosts
print READY. The phase-batch skill now requires the check before launch.
The scratch-commit point is skipped for the read-only mode, which cannot commit
by design.
Scratch git calls drop every inherited `GIT_*` variable and ignore global and
system git config, so no hook (including a global `core.hooksPath`) runs.
