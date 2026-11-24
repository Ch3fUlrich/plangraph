//! Proof that no confinement scope of a run was ever started, from the cgroup tree.
use pg_worker_core::run::{
    group::unit_name,
    slot_bind::{StartProof, Started},
};
use std::path::{Path, PathBuf};

/// Scopes live at an unknown depth below the cgroup root (user and system slices), so the
/// whole tree is searched; nothing is proven when any directory cannot be read.
pub(crate) struct CgroupScopes {
    root: PathBuf,
}
impl CgroupScopes {
    pub(crate) fn host() -> Self {
        Self::new("/sys/fs/cgroup")
    }
    pub(crate) fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }
}
/// `Some(true)` when a directory named `want` exists below `dir`, `None` when a directory
/// could not be read.
fn contains(dir: &Path, want: &str, depth: u32) -> Option<bool> {
    if depth > 12 {
        return None;
    }
    let mut unknown = false;
    for entry in std::fs::read_dir(dir).ok()? {
        let Ok(entry) = entry else {
            unknown = true;
            continue;
        };
        let Ok(kind) = entry.file_type() else {
            unknown = true;
            continue;
        };
        if !kind.is_dir() {
            continue;
        }
        if entry.file_name() == want {
            return Some(true);
        }
        match contains(&entry.path(), want, depth + 1) {
            Some(true) => return Some(true),
            Some(false) => {}
            None => unknown = true,
        }
    }
    if unknown {
        None
    } else {
        Some(false)
    }
}
impl StartProof for CgroupScopes {
    fn scope_started(&self, run_id: &str) -> Started {
        match contains(&self.root, &format!("{}.scope", unit_name(run_id)), 0) {
            Some(true) => Started::Yes,
            Some(false) => Started::No,
            None => Started::Unknown,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn a_scope_anywhere_below_the_root_is_a_started_run() {
        let d = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(d.path().join("user.slice/user-1.slice/pg-run-a.scope")).unwrap();
        std::fs::create_dir_all(d.path().join("system.slice")).unwrap();
        let probe = CgroupScopes::new(d.path());
        assert_eq!(probe.scope_started("run-a"), Started::Yes);
        assert_eq!(probe.scope_started("run-b"), Started::No);
    }
    #[test]
    fn an_unreadable_tree_proves_nothing() {
        let d = tempfile::tempdir().unwrap();
        let probe = CgroupScopes::new(d.path().join("missing"));
        assert_eq!(probe.scope_started("run-a"), Started::Unknown);
    }
}
