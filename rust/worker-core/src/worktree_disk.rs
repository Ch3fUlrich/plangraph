//! Reclaim legacy per-worktree Cargo output during the worktree sweep.
use std::{fs, io, path::Path};

pub(crate) fn remove_legacy_target(worktree: &Path) -> io::Result<()> {
    let rust = worktree.join("rust");
    match fs::symlink_metadata(&rust) {
        Ok(meta) if !meta.is_dir() => return Ok(()),
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(e) => return Err(e),
        _ => {}
    }
    let target = rust.join("target");
    match fs::symlink_metadata(&target) {
        Ok(meta) if meta.is_dir() => fs::remove_dir_all(target),
        Ok(_) => fs::remove_file(target),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(e),
    }
}

#[cfg(test)]
#[path = "worktree_disk_tests.rs"]
mod tests;
