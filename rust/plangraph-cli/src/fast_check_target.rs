use super::Result;
use pg_worker_core::cargo_pool;
use std::path::{Path, PathBuf};

pub(super) fn jail_binary(target: &Path) -> PathBuf {
    target
        .join("debug")
        .join(format!("pg-jail{}", std::env::consts::EXE_SUFFIX))
}

pub(super) fn resolve_cargo_target(
    root: &Path,
    primary: &Path,
    run: &str,
) -> Result<cargo_pool::SlotLease> {
    let worktree = root.canonicalize().map_err(|e| e.to_string())?;
    let slots = cargo_pool::pool_slots(primary, cargo_pool::DEFAULT_POOL_SLOTS)
        .map_err(|e| e.to_string())?;
    for path in &slots {
        if path.starts_with(&worktree)
            || worktree.starts_with(path)
            || path
                .components()
                .any(|c| c == std::path::Component::ParentDir)
            || path.ancestors().any(Path::is_symlink)
        {
            return Err(
                "cargo pool must be outside the checkout and must not traverse symlinks".into(),
            );
        }
    }
    cargo_pool::acquire_slot(primary, slots.len(), run, None)
        .map_err(|e| format!("cargo pool: {e}"))
}

#[cfg(unix)]
#[test]
fn resolve_cargo_target_uses_shared_pool_instead_of_worktree_target() {
    let tmp = tempfile::tempdir().unwrap();
    let worktree = tmp.path().join("wt");
    std::fs::create_dir_all(&worktree).unwrap();
    let primary = tmp.path().join("shared/cargo-target");

    let lease = resolve_cargo_target(&worktree, &primary, "fc-test").unwrap();
    assert_eq!(lease.path, primary);
    let second = resolve_cargo_target(&worktree, &primary, "fc-test-2").unwrap();
    assert_ne!(second.path, primary);
    assert!(!worktree.join("rust/target").exists());
    assert_eq!(
        jail_binary(&lease.path),
        primary.join(format!("debug/pg-jail{}", std::env::consts::EXE_SUFFIX))
    );
}
