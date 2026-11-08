//! Shared production startup reconciliation lock path.
pub(crate) fn path(root: &std::path::Path) -> std::path::PathBuf {
    root.join("reconcile.lock")
}
