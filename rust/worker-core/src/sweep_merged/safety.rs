//! Worktree inventory and fail-closed removal guards.
use super::{origin_contains, Options};
use crate::{
    disk_hygiene::{git, has_ignored_non_cache},
    prod_paths,
};
use std::{
    fs, io,
    path::{Path, PathBuf},
};

#[derive(Clone)]
pub(super) struct Tree {
    pub(super) path: PathBuf,
    pub(super) sha: String,
    pub(super) branch: String,
    pub(super) protected: bool,
}

pub(super) fn trees(repo: &Path) -> io::Result<Vec<Tree>> {
    let listing = git(repo, &["worktree", "list", "--porcelain", "-z"])?;
    let mut result = vec![];
    for (i, block) in listing.split("\0\0").filter(|b| !b.is_empty()).enumerate() {
        let fields: Vec<_> = block.split('\0').collect();
        let field = |prefix| {
            fields
                .iter()
                .find_map(|s| s.strip_prefix(prefix))
                .unwrap_or("")
                .to_string()
        };
        result.push(Tree {
            path: PathBuf::from(field("worktree ")),
            sha: field("HEAD "),
            branch: field("branch ").trim_start_matches("refs/heads/").into(),
            protected: i == 0
                || fields
                    .iter()
                    .any(|s| s.starts_with("locked") || s.starts_with("prunable")),
        });
    }
    Ok(result)
}
fn process_uses(path: &Path) -> io::Result<bool> {
    #[cfg(target_os = "linux")]
    {
        for entry in fs::read_dir("/proc")? {
            let entry = entry?;
            if !entry
                .file_name()
                .to_string_lossy()
                .bytes()
                .all(|b| b.is_ascii_digit())
            {
                continue;
            }
            match fs::read_link(entry.path().join("cwd")) {
                Ok(cwd) if cwd.starts_with(path) => return Ok(true),
                Ok(_) => {}
                Err(e) if e.kind() == io::ErrorKind::NotFound => {}
                Err(e) => return Err(e),
            }
        }
        Ok(false)
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = path;
        Err(io::Error::other("process cwd scan requires Linux"))
    }
}
pub(super) fn safe_tree(
    tree: &Tree,
    opts: &Options,
    refs: &[prod_paths::Reference],
) -> io::Result<()> {
    let path = tree.path.canonicalize()?;
    let root = opts.root.canonicalize()?;
    if tree.protected
        || path == root
        || path == opts.repo.canonicalize()?
        || !path.starts_with(root)
        || fs::symlink_metadata(&tree.path)?.file_type().is_symlink()
    {
        return Err(io::Error::other("protected or outside root"));
    }
    if refs.iter().any(|r| {
        let p = r.path.canonicalize().unwrap_or_else(|_| r.path.clone());
        p.starts_with(&path) || path.starts_with(&p)
    }) {
        return Err(io::Error::other("user unit reference"));
    }
    if process_uses(&path)? {
        return Err(io::Error::other("process cwd"));
    }
    if !git(
        &path,
        &["status", "--porcelain=v1", "--untracked-files=all"],
    )?
    .trim()
    .is_empty()
        || !git(&path, &["ls-files", "--", "target", "node_modules"])?
            .trim()
            .is_empty()
        || has_ignored_non_cache(&path)?
    {
        return Err(io::Error::other(
            "dirty, tracked cache or ignored non-cache files",
        ));
    }
    if !origin_contains(&opts.repo, &tree.sha)? {
        return Err(io::Error::other("commits missing from origin"));
    }
    Ok(())
}
