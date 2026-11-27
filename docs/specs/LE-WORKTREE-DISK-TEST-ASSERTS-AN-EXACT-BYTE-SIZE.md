# LE-WORKTREE-DISK-TEST-ASSERTS-AN-EXACT-BYTE-SIZE

Outcome. The shared Cargo cache cap test accepts the filesystem's allocation slack for the three writer files and still fails when the measured size misses, adds or double-counts a file.

- plan_source. Keeper item plangraph:LE-WORKTREE-DISK-TEST-ASSERTS-AN-EXACT-BYTE-SIZE, 2026-11-27 14:05Z, priority 10.
- base commit. 443bf493af86a2ccd1d2859fb4e1d52eceea522d
- card SHA-256. f924fd45392a73db2a79f464dabaf211062dd827e12fdde095e8b143d6394941
- tree SHA-256. aebcfb75eff9fbacb4890087add06707bbc86b11d381a75006d9e1592e77cd30
- evidence_after_deploy. No deployment (test-only). Over 7 days, record that fast-check logs on coding.vm, dell and the workstation show no `pg-worker-core/test` failure of `worktree::disk::tests::parallel_writers_are_stopped_at_shared_cap_and_idle_cache_is_reclaimed`.

## Checked against base

- The test asserts `result["size"] == 12.0 / 1024.0` GiB exactly (`rust/worker-core/src/worktree_disk_tests.rs:165`). The size comes from `plangraph.spawner.host_ready.cargo_target_size_gb` (`rust/worker-core/src/worktree_disk_tests.rs:118`).
- `cargo_target_size_gb` sums allocated bytes, `st_blocks * 512`, per regular file and skips symlinks and repeated hard links (`src/plangraph/spawner/host_ready.py:150-174`). Allocated bytes are the production measure for the cap, so the production code stays.
- Each of the 3 writer processes writes one 4 MiB file (`worktree_disk_tests.rs:178`, `disk_test_writer_process`), so the apparent total is 12 MiB = 12,582,912 bytes. The coding.vm failure measured 0.011730194091796877 GiB = 12,595,200 bytes: 12,288 bytes (4 KiB per file) of block allocation above the apparent size.
- Class search over Rust tests that compare a measured disk size: `rust/worker-core/src/disk_watermark.rs` and `cargo_pool_trim.rs` sum `blocks() * 512`, but their tests compare slot orders and injected sizes, not a measured total. `worktree_disk_cap_tests.rs` and `worktree_disk_reap_tests.rs` assert no size. This test is the only exact comparison on a measured allocation.

## Decisions

- D1. Keep the production measure (allocated bytes). The test compares the measured byte count against the apparent bytes the writers wrote, with a per-file slack bound. The fixture forms `files = 3` and `file_bytes = 4 MiB`, and the bound is `files * file_bytes <= measured <= files * (file_bytes + SLACK_PER_FILE)` with `SLACK_PER_FILE = 64 KiB`. 64 KiB covers one indirect or extent block and directory-free allocation rounding on ext4, xfs and the WSL ext4 disk (observed +4 KiB per file), and it is far below one 4 MiB file, so a missing, extra or double-counted file falls outside.
- D2. A measured size below the apparent bytes fails. Sparse or compressed allocation is not expected on the three hosts' work disks. A future host with compression needs its own decision, not a looser bound.
- D3. The Python bridge prints the size in bytes (`round(size_gb * 1024**3)`), so the Rust side compares integers instead of floats.
- D4. Put the bound in one pure helper, `fn cache_size_within_slack(measured: u64, files: u64, file_bytes: u64) -> bool`, in the same test module. A unit test pins it without the filesystem.

## Entry points

- E1. `worktree::disk::tests::parallel_writers_are_stopped_at_shared_cap_and_idle_cache_is_reclaimed`.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | A measured cache of 12 MiB plus 12 KiB (the coding.vm reading) and of exactly 12 MiB both pass. | X1 X2 |
| B2 | E1 | 8 MiB (one writer missing), 16 MiB (one file counted twice) and 12 MiB minus 4 KiB fail. | X1 |
| B3 | E1 | Statuses, refusal reasons, busy and idle results and reclaim assertions are unchanged. | X2 |

## Verification

Write X1 first. On the base it does not compile (the helper is missing). Then change X2.

- X1. `worktree::disk::tests::cache_size_check_allows_allocation_slack_and_rejects_wrong_counts`. Call the helper with `files = 3` and `file_bytes = 4 * 1024 * 1024`. Assert B1 for 12,582,912 and 12,595,200, and assert B2 for 8,388,608, 16,777,216 and 12,578,816. Assert the exact upper edge `3 * (4 MiB + 64 KiB)` passes and one byte above fails.
- X2. `worktree::disk::tests::parallel_writers_are_stopped_at_shared_cap_and_idle_cache_is_reclaimed`, changed. Replace the float equality with `assert!(cache_size_within_slack(size, 3, 4 * 1024 * 1024), "{result}")` on the integer byte size from D3.
- X3. Held out: `cargo test --manifest-path rust/Cargo.toml --offline --locked -p pg-worker-core --lib worktree::disk`, which runs the cap and reap submodules unchanged.

## Scope and reuse

- R1. One file changes: `rust/worker-core/src/worktree_disk_tests.rs` (398 lines; at most 30 lines added).
- N1. No production change, no change to `host_ready.py`. Test-only: commit with `Changelog: skip (test-only fix)`, no fragment.

### Implementation

- In the embedded Python, print `size=round(h.cargo_target_size_gb(target) * 1024 ** 3)`.
- Add the `SLACK_PER_FILE` constant with a comment naming block allocation as the reason, the helper, and X1 next to the test.

## Open points for design approval

None.
