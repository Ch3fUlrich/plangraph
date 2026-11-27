# LE-SPAWNED-CODEX-WRITER-CANNOT-COMMIT-OR-BUILD-IN-A-LANE-WORKTREE

Outcome. A plain Codex implement or fix spawn runs `workspace-write` with its worktree git dir, the git common dir, the shared Cargo target and its run directory as added writable roots, and is refused before launch when those roots cannot be resolved or written.

- plan_source. Keeper item plangraph:LE-SPAWNED-CODEX-WRITER-CANNOT-COMMIT-OR-BUILD-IN-A-LANE-WORKTREE, 2026-11-27, priority 10, incident 15:47Z. Operator approval of Codex `--add-dir` from 2026-11-26.
- base commit. 443bf493af86a2ccd1d2859fb4e1d52eceea522d
- card SHA-256. 9eb4e77d926b270c618f91c88b9d00f06c4b8c13cac309f21fa3484fd3bc3852
- tree SHA-256. d14c03af2f5a1f708d7210bc5726df84a56fac98beea3e1fc31ffad65cdd3335
- evidence_after_deploy. Take this on one host first (rule 3.112 (1)), the workstation guest, then dell as its own step. Remove `[sandbox_workspace_write] writable_roots` from `~/.codex/config.toml` (backup first), then spawn one plain `cx-gpt-6.2-sol-high` implement run in a lane worktree. Record its argv (`--sandbox workspace-write` and the `--add-dir` list), its commit SHA and its successful Cargo build. Removing the stopgap is host work for the host session.

## Checked against base

- For harness `codex`, `build_standalone_fallback_command` emits `--sandbox workspace-write` and one `--add-dir` per entry only when `writable_dirs` is non-empty and the role is not review. Otherwise `codex exec` runs with its default read-only sandbox. [src/plangraph/routing_support/clients.py:424–430]
- `SpawnerRunner.spawn` passes its `writable_dirs` argument (default `()`) to `build_command`. No spawn path sets it for implement or fix runs. Only the leg probe passes `<root>/.git` for Codex writer probes. [src/plangraph/spawner/runner.py:186,264–273; src/plangraph/spawner/leg_probe.py:283–289]
- So a plain Codex implement spawn runs read-only. The 15:3xZ runs run-a58f6f7c and run-0e76727f could not write outside cwd, and run-dcae82c3 could not write cwd at all. Even with `workspace-write`, a lane worktree keeps its index and objects under the main checkout's `.git` (`.git/worktrees/<lane>` and the common dir), and `CARGO_TARGET_DIR` is the shared target that `cargo_child_env` pins for non-review roles. Both lie outside the workspace root (runs 22140068, d5c7c3db and dc47c700). [src/plangraph/spawner/runner.py:277–286; src/plangraph/spawner/host_ready.py:79–93]
- The native writer already grants the shared Cargo target, its review directory, its run directory and both git dirs as writable roots. That is the working path today. [rust/pg-spawnd/src/native_writer_host.rs:174–215; rust/pg-spawnd/src/native_writer_sandbox.rs:97–117]
- `runner.py` is over the 800-line limit on the baseline (1157) and may not grow. `git_guard.py` (393 lines) already holds the writer-only launch helper `writer_launch`, which `runner.py:42` imports. [.filesize-baseline:53; src/plangraph/spawner/git_guard.py:383–393]

## Decisions

- D1. Roots. `git_guard.writer_roots(role, client, work_dir, writable_dirs, prompt_record)` returns `tuple(writable_dirs)` unchanged unless `role in ('implement', 'fix')` and `client == 'codex'`. In that case it returns, de-duplicated and in this order: the caller's `writable_dirs`, then the absolute `git rev-parse --path-format=absolute --git-dir` and `--git-common-dir` of `work_dir` (run with `checkout_env()`, 10-second timeout), then `host_ready.shared_cargo_target()`, then the run directory `prompt_record[0].parent` when `prompt_record` is given. These are the same root kinds the native writer grants. The review directory is left out because plain spawns have none.
- D2. Refusal before launch. `writer_roots` raises `SpawnRefused('writer_sandbox_unwritable: <what>')` before any process starts in three cases: the git dirs cannot be resolved (`work_dir is not a git checkout`); a git dir is not writable by the current user (`os.access(path, os.W_OK)`); or the nearest existing ancestor of the Cargo target is not writable. `<what>` names the path kind (`git dir`, `git common dir`, `cargo target`) and the path. Every Codex implement or fix spawn then gets a non-empty root list, so the read-only case cannot reach `codex exec`.
- D3. Wiring with no growth in `runner.py`. Line 42 imports `writer_roots` beside `writer_launch`. The `build_command` call's line `client=client, writable_dirs=writable_dirs,` becomes `client=client, writable_dirs=writer_roots(role, client, work_dir, writable_dirs, prompt_record),`. `client` here is the leg harness that `resolve_launch_target` already settled.
- D4. Leave `clients.py` unchanged. Its rule ("workspace-write when writable dirs are given, never for review") is right once callers pass the roots. The leg probe keeps passing `<root>/.git` and is not routed through `writer_roots`, because its role and client already make it a Codex implement call and D1 would add the shared target to a disposable probe repo.
- D5. The host stopgap (`writable_roots` in `~/.codex/config.toml` on the workstation and dell) is removed after deployment as a host step (evidence_after_deploy), not by this change.

## Entry points

- E1. `SpawnerRunner.spawn` → `build_command` for any implement or fix spawn on harness `codex` (CLI `plangraph-spawn`, MCP `plangraph_agent_spawn`, feeder plain spawns).

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | A Codex implement spawn whose cwd is a `git worktree add` lane worktree builds argv with `--sandbox workspace-write`, `--add-dir <repo>/.git/worktrees/<lane>`, `--add-dir <repo>/.git`, `--add-dir <shared target>` and `--add-dir <run dir>`. | X1 X2 |
| B2 | E1 | A Codex review spawn and a Claude or OpenCode implement spawn build argv byte-identical to the base. | X1 |
| B3 | E1 | A Codex implement spawn whose cwd is not a git checkout, or whose git common dir or Cargo target ancestor is not writable, raises `SpawnRefused` starting `writer_sandbox_unwritable:` and starts no process. | X1 X2 |
| S1 | E1 | With the host stopgap removed, a plain spawned writer commits and builds in a lane worktree on the workstation guest, and then on dell. | X3 |

## Verification

- X1. E1, changed. `tests/test_writer_sandbox_roots.py::test_codex_writer_in_a_lane_worktree_gets_both_git_dirs_the_target_and_the_run_dir`, `test_reviews_and_other_harnesses_are_unchanged` and `test_unresolvable_or_unwritable_roots_refuse` (the not-writable cases set mode 0o500 and are skipped when running as root). Call `writer_roots` directly, then `clients.build_command(..., client='codex', writable_dirs=...)`. Use a temporary repository with `git worktree add` and `PLANGRAPH_CARGO_TARGET_DIR` set to a temporary path.
- X2. E1, changed. `tests/test_writer_sandbox_roots.py::test_runner_spawn_passes_writer_roots_to_codex`. Use the stub `process_factory` pattern of `tests/test_claude_child_env.py` (lines 115–131) to capture argv, with the harness-availability seams (`codex_review_client`, `resolver.host_skip_reason_async`) monkeypatched to admit. Assert B1 on the captured argv and B3 by asserting the factory is never called. Check for X1 and X2: `python3 -m pytest -q tests/test_writer_sandbox_roots.py`.
- X3. Live only. The evidence_after_deploy observation.
- X4. Held out. `python3 -m pytest -q tests/test_claude_child_env.py tests/test_leg_probe_w1.py`, unchanged and green.

Write X1 and X2 first. B1 and B3 fail on the base, which emits no sandbox flags.

## Scope and reuse

- R1. Reuse `shared_cargo_target`, `checkout_env`, `SpawnRefused` and the existing `writable_dirs` channel to `clients.py`. One root function serves every plain spawn path.
- N1. The native writer, review seats, the leg probe and `~/.codex/config.toml` are not changed. `CARGO_HOME` is not added: the item and the native writer grant only the target, and no incident shows a `CARGO_HOME` write refusal.

### File budgets

`runner.py` stays at 1157 lines (two lines edited in place). `git_guard.py` 393 → at most 443 lines. The change is documented in the fragment `docs/spawner/LE-SPAWNED-CODEX-WRITER-CANNOT-COMMIT-OR-BUILD-IN-A-LANE-WORKTREE.md`.

### Implementation

- `git_guard.py`: add `writer_roots` per D1 and D2, after `writer_launch`. Paths are `str` of absolute paths. De-duplicate by resolved path and keep the first spelling.
- `runner.py`: D3.

## Open points for design approval

None.
