//! Linux procfs cwd checks immediately before destructive worktree operations.
use std::{fs, io, path::Path};
fn identity(path: &Path) -> io::Result<Option<u64>> {
    let stat = fs::read_to_string(path.join("stat"))?;
    let fields = stat
        .rsplit_once(')')
        .ok_or_else(|| io::Error::other("invalid proc stat"))?
        .1
        .split_whitespace()
        .collect::<Vec<_>>();
    if matches!(fields.first(), Some(&"Z" | &"X")) {
        return Ok(None);
    }
    let start = fields
        .get(19)
        .and_then(|s| s.parse().ok())
        .ok_or_else(|| io::Error::other("invalid proc start"))?;
    Ok(Some(start))
}
pub fn tree_in_use(proc_root: &Path, tree: &Path) -> io::Result<bool> {
    let mut unreadable = false;
    for entry in fs::read_dir(proc_root)? {
        let entry = entry?;
        if !entry
            .file_name()
            .to_string_lossy()
            .bytes()
            .all(|b| b.is_ascii_digit())
        {
            continue;
        }
        let sample = || -> io::Result<bool> {
            let before = identity(&entry.path())?;
            if before.is_none() {
                return Ok(false);
            }
            let cwd = fs::read_link(entry.path().join("cwd"))?;
            let after = identity(&entry.path())?;
            Ok(before == after && cwd.starts_with(tree))
        };
        match sample() {
            Ok(true) => return Ok(true),
            Ok(false) => {}
            Err(e) if e.kind() == io::ErrorKind::NotFound => {}
            Err(e) if e.kind() == io::ErrorKind::PermissionDenied => unreadable = true,
            Err(e) => return Err(e),
        }
    }
    if unreadable {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "process_visibility_incomplete",
        ));
    }
    Ok(false)
}
