# LE-FAST-CHECKS-FAIL-IN-EVERY-NON-PLANGRAPH-REPOSITORY

Outcome. `plangraph fast-checks` in a repository whose base has no `tools/ci_select.py` runs the declared check from the base, or only `git diff --check` with a warning, so native writer heads in other projects can get a green receipt and no head can weaken its own gate.

- plan_source. Keeper item plangraph:LE-FAST-CHECKS-FAIL-IN-EVERY-NON-PLANGRAPH-REPOSITORY, 2026-11-27, priority 10, D-7047.
- base commit. 443bf493af86a2ccd1d2859fb4e1d52eceea522d
- card SHA-256. 96504d08abc30766998b4c4815f8efcf6d3dfc9aa1bdef38519191e66fc38cef
- tree SHA-256. de2fe8581c086285668714856c9224becb093fbde25f7f29bef2b7ed2741c992
- evidence_after_deploy. After the CLI deploy on the workstation, record one basic-analysis native writer run whose `fast_checks.status` is `passed`, whose receipt reads `result green` with the `diff-check` or `repository-check` line, and whose review started.

## Checked against base

- `plan()` always runs `python3 tools/ci_select.py select --files ...` first (`rust/plangraph-cli/src/fast_checks.rs:120-128`). In a checkout without that file, `output()` fails and the run ends red before any step (`fast_checks.rs:104-119`).
- pg-spawnd runs `plangraph fast-checks --owned-worktree` for every native writer, whatever the repository (`rust/pg-spawnd/src/native_writer_host.rs:108-114,391-430`). basic-analysis runs `run-df783f0d` and `run-b4555b72` ended no_commit on this.
- The other plangraph steps also need plangraph files (`tools/split/scrub_check.py`, `tools/check_file_size.py`, `tools/ci_commit_gate.py` and others; `fast_checks.rs:136-170`). Only `git diff --check <base>` is repository-neutral.
- `execute()` always takes a Cargo pool slot before the first step (`fast_checks.rs:425-426`), even when no step runs Cargo.
- The owned parent turns the child's `check` lines into the receipt (`rust/plangraph-cli/src/fast_check_worktree.rs:27-91`). It needs no change.

## Decisions

- D1. The gate is chosen from the base, not from the head. Compute `mb = git merge-base <base> HEAD`, the same merge-base `diff_paths` already uses. The mode is:
  - `Selector`, when the checkout has `tools/ci_select.py` or `mb:tools/ci_select.py` exists (`git cat-file -e`). The plan is unchanged. A head that deletes the selector in a repository whose base has it still runs the selector step. That step fails, so the receipt is red (fail closed).
  - `Declared`, when there is no selector in either and `mb:.plangraph/fast-checks` exists. The script content comes from the merge-base, never from the head, so a head cannot weaken its own gate. A head that edits or deletes the script still runs the base version. A change to the script takes effect once it is on the base branch.
  - `None` otherwise.
  When the checkout lacks the selector and `mb` cannot be resolved, the run fails with `cannot resolve base <base> to choose the gate` (red, fail closed). A plangraph checkout with the selector never needs `mb`, so `--plan` keeps working without `origin/main`.
- D2. In `Declared` mode, `plan()` writes the blob of `mb:.plangraph/fast-checks` (`git cat-file blob`) to `<scratch>/repository-check` with mode 0755. `<scratch>` is a `tempfile::TempDir` that `execute()` (or the `--plan` path) creates and holds until the steps end. The plan is one step, `repository-check: <scratch>/repository-check <base>`, and the selection text is `selector=repository\n`. The script runs with the checkout as working directory and the base revision as its only argument. The absolute path avoids the platform-dependent resolution of a relative program path.
- D3. In `None` mode, the plan is one step, `diff-check: git diff --check <base>`. The selection text is two lines: `selector=none` and `WARNING: no tools/ci_select.py or .plangraph/fast-checks on the base; only git diff --check ran; the repository's CI judges the pull request`. A head that adds a new `.plangraph/fast-checks` stays in `None` mode until that file is on the base.
- D4. The Cargo pool slot is taken exactly in `Selector` mode, as today. The plangraph plan can build `pg-policy` with Cargo from a Python step (`tools/check_new_code_lang.py:14-33`, step at `fast_checks.rs:148-150`), so the lease cannot be inferred from step program names. `Declared` and `None` take no slot. Their steps run through `pg_worker_core::stall::spawn_retrying` with no `CARGO_TARGET_DIR`, `CARGO_INCREMENTAL` or `PG_JAIL_BIN`. The base, receipt and budget handling are unchanged.
- D5. The labels `repository-check` and `diff-check` go into the receipt like every other step label, through the unchanged owned-worktree parent. Consumers need no change.

## Entry points

- E1. `fast_checks::plan` through `plangraph fast-checks --plan [--base B] --files ...`.
- E2. `fast_checks::execute` through `plangraph fast-checks --owned-worktree [--base B]`, the entry point that produces the receipt.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | A repository with neither selector nor declared check on the base prints `selector=none`, the WARNING line and exactly one step, `diff-check: git diff --check <base>`. | X1 |
| B2 | E1 | A repository whose base has an executable `.plangraph/fast-checks` prints `selector=repository` and exactly one step, `repository-check: <scratch>/repository-check <base>`. | X2 |
| B3 | E1 | The plangraph checkout prints the ci_select booleans and the plangraph steps (`scrub:`, `file-size:`, `diff-check:`) and no `selector=` line. | X3 |
| B4 | E2 | An owned run in `None` mode publishes `result green` with `check diff-check rc=0` and exits 0, without touching the Cargo pool. | X4 |
| B5 | E2 | An owned run in `Declared` mode runs the base's script with the base as argument. Exit 0 publishes `result green` with `check repository-check rc=0`. A script exit of 3 publishes `result red` with `check repository-check rc=3`, and the run exits 1. | X5 |
| B6 | E1 E2 | A head that edits the base's failing script to `exit 0` still gets a red receipt. A head that deletes `tools/ci_select.py` from a base that has it gets a red receipt. | X6 |

## Verification

Write X1-X6 first. On the base, X1, X2, X4 and X5 fail on `python3: can't open file 'tools/ci_select.py'`, and X6's edited-script case passes wrongly once a head-chosen gate exists.

All tests are in the new file `rust/plangraph-cli/tests/fast_checks_foreign_repo.rs`. Use `git init` temp repositories with a fixture identity, like `rust/plangraph-cli/tests/fast_checks.rs::git`, and run `env!("CARGO_BIN_EXE_plangraph")`. Owned runs set `HOME` to a temp dir and `PLANGRAPH_CARGO_TARGET_DIR` to a path below a regular file, so any pool use fails. They pass `--base <base sha>`. Read a receipt with `git show refs/plangraph/receipts/<head>:<head>` (the ref of `fast_check_stamp::local_receipt_ref`). Unix only (`#[cfg(unix)]`).

- X1. `a_repository_without_selector_or_declared_check_plans_one_diff_check_step`. `--plan --base HEAD --files a.txt`, exit 0, assert B1.
- X2. `a_declared_check_on_the_base_is_the_one_step`. Commit `.plangraph/fast-checks` with mode 0755, then assert B2 with `--plan --base HEAD`.
- X3. `the_plangraph_checkout_keeps_the_selector_plan`. Run from `CARGO_MANIFEST_DIR/../..` with `--plan --files README.md` and assert B3.
- X4. `an_owned_run_without_a_declared_check_gets_a_green_diff_check_receipt`. Use a base commit plus a head commit changing `a.txt` and assert B4.
- X5. `an_owned_run_uses_the_base_declared_check_and_records_its_verdict`. The base commits `.plangraph/fast-checks`: `#!/bin/sh`, `git merge-base --is-ancestor "$1" HEAD || exit 9`, `exit "$(cat rc 2>/dev/null || echo 0)"`. Head 1 adds `rc` = `0` and head 2 sets `rc` = `3`. Assert B5 for both heads.
- X6. `a_head_cannot_weaken_its_gate`. (a) The base script exits 3, and the head rewrites it to `exit 0`: red receipt, exit 1. (b) The base has a stub `tools/ci_select.py` (prints `pytest=false` lines), and the head deletes it: red receipt, exit 1.
- X7. Held out: `cargo test --manifest-path rust/Cargo.toml --offline --locked -p plangraph-cli --test fast_checks`, unchanged.

## Scope and reuse

- R1. One plan function and one step loop. The foreign plan is a branch at the top of `plan()`. No second fast-checks entry and no change to the owned-worktree parent.
- N1. No change to pg-spawnd: the native writer host keeps running `plangraph fast-checks --owned-worktree` everywhere.
- N2. Writing a `.plangraph/fast-checks` for basic-analysis is that repository's work, not this item's.

### File budgets

- `rust/plangraph-cli/src/fast_checks.rs`: 500 lines, at most 70 lines added.
- `rust/plangraph-cli/tests/fast_checks_foreign_repo.rs`: new, at most 260 lines.
- `docs/contributing/LE-FAST-CHECKS-FAIL-IN-EVERY-NON-PLANGRAPH-REPOSITORY.md`: new, at most 8 lines, stating D1-D4 for the "Fast local CI" section of CONTRIBUTING.md (800 lines, full).
- `changelog.d/LE-FAST-CHECKS-FAIL-IN-EVERY-NON-PLANGRAPH-REPOSITORY.md`: new, at most 4 lines.

### Implementation

- Add `enum Gate { Selector, Declared, None }` and `fn gate(root, base) -> Result<Gate>` per D1. `plan(root, base, paths, scratch: &Path)` dispatches on it. `foreign_plan` covers D2 and D3.
- `execute()`: create the scratch `TempDir`, compute the gate once, and resolve `cargo_cache::shared_cache_roots()` and `resolve_cargo_target` only for `Selector`, holding `Option<SlotLease>`. In the loop, set the Cargo environment and call `run_step` when the lease is present. Otherwise run `spawn_retrying(&mut command)?.wait()`.
- The `--plan` path creates its own scratch dir, so `plan()` has one signature.

## Open points for design approval

None.

## Open review points for the implementer (round 2, final under rule 3.119, D-7092)

The spec is final after two design review rounds (gpt-6.2-sol-high). The points below were still open; the implementer resolves them inside the write set or reports why not.

VERDICT: DESIGN-CHANGE
CITATIONS: checked 8, wrong 0
ROUND 1: D1–D3 still open. D4 and D5 fixed with selector-only leasing and owned receipt cases for both foreign modes.
ITEM FIT: Adds a declared check or diff fallback for foreign repositories and preserves the plangraph lease. Trusted gate selection remains incomplete.
POINTS: D1–D3, spec:21-27,45,58, rust/plangraph-cli/src/fast_checks.rs:120-132. The checkout selector still takes precedence over a base-declared script, and the head’s selector still supplies required-suite booleans. Choose execution mode from the pinned base, preserve the base’s required suites, and confine checkout-only detection to interactive planning without a resolvable base. Add a case where the head adds a selector over a failing base-declared check. BLOCKS
