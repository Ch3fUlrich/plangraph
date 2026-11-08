//! Acquire the record lock before reading the record used for watcher attachment.
use pg_worker_core::lockfile::LockGuard;
use std::path::Path;

pub(super) fn attach_record(
    root: &Path,
    run: &str,
) -> Option<(LockGuard, pg_worker_core::run::record::RunRecord)> {
    let dir = root.join("runs").join(run);
    let lock = crate::reconcile_store::lock(&dir, &dir.join("record.lock")).ok()?;
    let record = pg_worker_core::run::record::RunRecord::read(root, run).ok()?;
    Some((lock, record))
}
