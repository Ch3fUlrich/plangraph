# LE-FAST-CHECKS-FAIL-IN-EVERY-NON-PLANGRAPH-REPOSITORY

Outcome. `plangraph fast-checks` in a checkout without `tools/ci_select.py` runs the repository's declared check, or only `git diff --check` with a warning, so native writer heads in other projects can get a green receipt.

- plan_source. Keeper item plangraph:LE-FAST-CHECKS-FAIL-IN-EVERY-NON-PLANGRAPH-REPOSITORY, 2026-11-27, priority 10, D-7047.
- base commit. 443bf493af86a2ccd1d2859fb4e1d52eceea522d
- card SHA-256. 18c500219aa026003a494d0a9dbf8148fcf5fdc38cee89422728893729fc08d8
- tree SHA-256. 737a973b6258eceb6a8e7dc23ab7bb0eec3ebb9e6c55adb84658944a6e79d38e
- evidence_after_deploy. After the CLI deploy on the workstation, record one basic-analysis native writer run whose `fast_checks.status` is `passed`, whose receipt reads `result green` with the `diff-check` or `repository-check` line, and whose review started.

## Checked against base

- `plan()` always runs `python3 tools/ci_select.py select --files ...` first (`rust/plangraph-cli/src/fast_checks.rs:120-128`). In a checkout without that file, `output()` fails and the run ends red before any step (`fast_checks.rs:104-119`).
- pg-spawnd runs `plangraph fast-checks --owned-worktree` for every native writer, whatever the repository (`rust/pg-spawnd/src/native_writer_host.rs:108-114,391-430`). basic-analysis runs `run-df783f0d` and `run-b4555b72` ended no_commit on this.
- The other plangraph steps also need plangraph files (`tools/split/scrub_check.py`, `tools/check_file_size.py`, `tools/ci_commit_gate.py` and others; `fast_checks.rs:136-170`). Only `git diff --check <base>` is repository-neutral.
- `execute()` always takes a Cargo pool slot before the first step (`fast_checks.rs:425-426`), even when no step runs Cargo.
- The owned parent turns the child's `check` lines into the receipt (`rust/plangraph-cli/src/fast_check_worktree.rs:27-91`). It needs no change.

## Decisions

- D1. Detect the selector by `root.join("tools/ci_select.py").is_file()`. With it, the plan is unchanged.
- D2. Without the selector, if `root.join(".plangraph/fast-checks").is_file()`, the plan is one step, `repository-check: <absolute root>/.plangraph/fast-checks <base>`. The selection text is `selector=repository\n`. The script gets the base revision as its only argument and runs with the checkout as working directory. The absolute path avoids the platform-dependent resolution of a relative program path. A non-executable file fails red with the OS error, and the repository owner fixes it.
- D3. Without both, the plan is one step, `diff-check: git diff --check <base>`. The selection text is two lines: `selector=none` and `WARNING: no tools/ci_select.py or .plangraph/fast-checks in this checkout; only git diff --check ran; the repository's CI judges the pull request`.
- D4. `execute()` takes a Cargo pool slot only when a planned step's program is `cargo`. Without a slot, a step runs through `pg_worker_core::stall::spawn_retrying` with no `CARGO_TARGET_DIR`, `CARGO_INCREMENTAL` or `PG_JAIL_BIN`. The base, receipt and budget handling are unchanged.
- D5. The labels `repository-check` and `diff-check` go into the receipt like every other step label. Consumers need no change.

## Entry points

- E1. `fast_checks::plan` through `plangraph fast-checks --plan --files ...`.
- E2. `fast_checks::execute` through `plangraph fast-checks [--base B]`, and so through `--owned-worktree`.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | A checkout without selector or declared check prints `selector=none`, the WARNING line and exactly one step, `diff-check: git diff --check origin/main`. | X1 |
| B2 | E1 | A checkout with an executable `.plangraph/fast-checks` prints `selector=repository` and exactly one step, `repository-check: <root>/.plangraph/fast-checks origin/main`. | X2 |
| B3 | E1 | The plangraph checkout prints the ci_select booleans and the plangraph steps (`scrub:`, `file-size:`, `diff-check:`) and no `selector=` line. | X3 |
| B4 | E2 | In a foreign checkout, the declared check runs with the base as argument and the run exits 0 without touching the Cargo pool. A failing script exits 1 with `failed: repository-check`. | X4 |

## Verification

Write X1-X4 first. On the base, X1, X2 and X4 fail on `python3: can't open file 'tools/ci_select.py'`.

All tests are in the new file `rust/plangraph-cli/tests/fast_checks_foreign_repo.rs`. Use `git init` temp repositories with a fixture identity, like `rust/plangraph-cli/tests/fast_checks.rs::git`, and run `env!("CARGO_BIN_EXE_plangraph")`.

- X1. `a_checkout_without_the_selector_plans_one_diff_check_step`. Assert B1 on `fast-checks --plan --files a.txt`, exit 0.
- X2. `a_declared_repository_check_is_the_one_step`. Write `.plangraph/fast-checks` with mode 0755 and assert B2.
- X3. `the_plangraph_checkout_keeps_the_selector_plan`. Run from `CARGO_MANIFEST_DIR/../..` with `--files README.md` and assert B3.
- X4. `a_foreign_checkout_runs_its_check_without_a_cargo_slot`. Commit a base, then commit a head adding `.plangraph/fast-checks` (`#!/bin/sh` that writes `$1` to `marker` and exits with the code in `rc`, default 0). Run `fast-checks --base <base sha>` with `HOME` set to a temp dir and `PLANGRAPH_CARGO_TARGET_DIR` set to a path below a regular file, so any pool use fails. Assert exit 0 and that `marker` holds the base sha. Then write `3` to `rc`, rerun, and assert exit 1 and stderr containing `failed: repository-check`. Unix only (`#[cfg(unix)]`).
- X5. Held out: `cargo test --manifest-path rust/Cargo.toml --offline --locked -p plangraph-cli --test fast_checks`, unchanged.

## Scope and reuse

- R1. One plan function and one step loop. The foreign plan is a branch at the top of `plan()`. No second fast-checks entry and no change to the owned-worktree parent.
- N1. No change to pg-spawnd: the native writer host keeps running `plangraph fast-checks --owned-worktree` everywhere.
- N2. Writing a `.plangraph/fast-checks` for basic-analysis is that repository's work, not this item's.

### File budgets

- `rust/plangraph-cli/src/fast_checks.rs`: 500 lines, at most 45 lines added.
- `rust/plangraph-cli/tests/fast_checks_foreign_repo.rs`: new, at most 170 lines.
- `docs/contributing/LE-FAST-CHECKS-FAIL-IN-EVERY-NON-PLANGRAPH-REPOSITORY.md`: new, at most 8 lines, stating D2-D4 for the "Fast local CI" section of CONTRIBUTING.md (800 lines, full).
- `changelog.d/LE-FAST-CHECKS-FAIL-IN-EVERY-NON-PLANGRAPH-REPOSITORY.md`: new, at most 4 lines.

### Implementation

- `plan()`: return early with `foreign_plan(root, base)` when D1 fails. `foreign_plan` returns `(selection, vec![step])` per D2 and D3.
- `execute()`: compute `needs_pool` from the steps. Resolve `cargo_cache::shared_cache_roots()` and `resolve_cargo_target` only when it is true, holding `Option<SlotLease>`. In the loop, set the Cargo environment and call `run_step` when the lease is present. Otherwise run `spawn_retrying(&mut command)?.wait()`.

## Open points for design approval

None.
