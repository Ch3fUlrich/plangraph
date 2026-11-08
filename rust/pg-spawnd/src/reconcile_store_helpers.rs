//! Shared record validation; callers decide row skip versus global abort.
use crate::reconcile::ReconcileError;
use serde_json::Value;
use std::path::Path;
pub(super) fn check(path: &Path, directory: bool) -> Result<(), ReconcileError> {
    let meta = std::fs::symlink_metadata(path).map_err(|_| ReconcileError::Io)?;
    if meta.file_type().is_symlink()
        || (directory && !meta.is_dir())
        || (!directory && !meta.is_file())
    {
        return Err(ReconcileError::Invalid);
    }
    Ok(())
}
pub(super) fn seconds(value: &Value) -> Result<u64, ReconcileError> {
    let n = value
        .as_f64()
        .filter(|n| n.is_finite() && *n >= 0.0)
        .ok_or(ReconcileError::Invalid)?
        * 1000.0;
    if !n.is_finite() || n >= u64::MAX as f64 {
        return Err(ReconcileError::Invalid);
    }
    Ok(n as u64)
}
