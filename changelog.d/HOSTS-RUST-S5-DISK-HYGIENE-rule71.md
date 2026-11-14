pg-disk-hygiene now uses configurable absolute free-space thresholds: alarm below 5 GB and cleanup below 10 GB, with no percentage logic. Shared Cargo targets evict old incremental sessions and stale build variants by age under pressure or above a configurable 10 GB cap, retaining the newest compile and run units per crate/profile and deferring to live builds.
Add a daily merged-worktree sweep using pg-reap policy, live protection readers and pre-deletion restore records; remote deletion is opt-in.
Recognize systemd working-directory flags and Cargo 1.100 nested build-script units when protecting worktrees and reclaiming stale shared cache variants.
Repair the standalone worker lockfile to include the pg-reap path dependency used by disk hygiene.
Honor the spawner's shared Cargo lease and configured cache root, reclaim legacy rust/target and Python caches, and align the alarm floor to GiB.
Exclude nested cache paths literally when probing ignored data so Git does not mistake their source-directory ancestors for valuable ignored files.
