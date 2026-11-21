//! Paths named by systemd user units and drop-ins, so cleanup spares what a unit runs from.
use std::{
    collections::BTreeSet,
    fs, io,
    path::{Path, PathBuf},
};

/// An absolute path a user unit or drop-in names, with the unit that names it.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct UnitRef {
    pub unit: String,
    pub path: PathBuf,
    /// True for a path the report may name (a whole word, or a path-valued field's value, see
    /// `words`); false for a guess that only protects.
    pub exact: bool,
}

/// `$XDG_CONFIG_HOME/systemd/user`, or `$HOME/.config/systemd/user`.
pub fn systemd_user_dir() -> io::Result<PathBuf> {
    let config = match std::env::var_os("XDG_CONFIG_HOME").filter(|v| !v.is_empty()) {
        Some(dir) => PathBuf::from(dir),
        None => PathBuf::from(
            std::env::var_os("HOME")
                .ok_or_else(|| io::Error::other("HOME unset: unit files unreadable"))?,
        )
        .join(".config"),
    };
    Ok(config.join("systemd/user"))
}

/// Every absolute path under the user unit directory: unit files and `*.d` drop-ins (any
/// line), and the targets of linked units. A missing directory names nothing; an unreadable
/// entry refuses (the caller protects everything).
pub fn unit_file_refs(dir: &Path, home: &Path) -> io::Result<Vec<UnitRef>> {
    let mut refs = BTreeSet::new();
    collect(dir, None, home, 0, &mut refs)?;
    Ok(refs.into_iter().collect())
}

fn collect(
    dir: &Path,
    owner: Option<&str>,
    home: &Path,
    depth: usize,
    refs: &mut BTreeSet<UnitRef>,
) -> io::Result<()> {
    let entries = match fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(e) => return Err(e),
    };
    for entry in entries {
        let entry = entry?;
        let path = entry.path();
        let name = entry.file_name().to_string_lossy().into_owned();
        let kind = fs::symlink_metadata(&path)?.file_type();
        if kind.is_symlink() {
            // A linked unit: the link target is itself a path the unit runs from.
            if let Ok(target) = fs::read_link(&path).map(|t| t.to_path_buf()) {
                if target.is_absolute() {
                    refs.insert(UnitRef {
                        unit: owner.unwrap_or(&name).to_string(),
                        path: target,
                        exact: true,
                    });
                }
            }
        }
        if kind.is_dir() {
            if depth >= 4 {
                return Err(io::Error::other("unit directory nesting too deep"));
            }
            let owner = name
                .strip_suffix(".d")
                .filter(|n| n.contains('.'))
                .or(owner);
            collect(&path, owner, home, depth + 1, refs)?;
        } else if fs::metadata(&path).is_ok_and(|m| m.is_file()) {
            let bytes = fs::read(&path)?;
            if bytes.len() > 1 << 20 {
                return Err(io::Error::other("unit file too large"));
            }
            let unit = owner.unwrap_or(&name).to_string();
            for (path, exact) in words::text_paths(&String::from_utf8_lossy(&bytes), home) {
                refs.insert(UnitRef {
                    unit: unit.clone(),
                    path,
                    exact,
                });
            }
        }
    }
    Ok(())
}

/// The worktree root a path lives in, when it lives in one: the first component below a
/// `*worktrees` directory, or `runs/<run>/worktree*` of a state directory.
fn worktree_root(path: &Path) -> Option<PathBuf> {
    let parts: Vec<_> = path.components().collect();
    let name = |i: usize| parts[i].as_os_str().to_string_lossy();
    for i in 0..parts.len() {
        if name(i).ends_with("worktrees") && i + 1 < parts.len() {
            return Some(parts.iter().take(i + 2).collect());
        }
        if name(i) == "runs" && i + 2 < parts.len() && name(i + 2).starts_with("worktree") {
            return Some(parts.iter().take(i + 3).collect());
        }
    }
    None
}

/// A ref whose worktree root is missing.
pub fn missing_worktree_refs(refs: &[UnitRef]) -> Vec<UnitRef> {
    refs.iter()
        .filter(|r| r.exact && worktree_root(&r.path).is_some_and(|root| !root.exists()))
        .cloned()
        .collect()
}

/// The units whose files name the worktree, or a path inside it.
pub fn unit_names(refs: &[UnitRef], tree: &Path, canonical: &Path) -> Vec<String> {
    let names: BTreeSet<_> = refs
        .iter()
        .filter(|r| {
            let p = r.path.canonicalize().unwrap_or_else(|_| r.path.clone());
            p.starts_with(canonical) || r.path.starts_with(tree)
        })
        .map(|r| r.unit.clone())
        .collect();
    names.into_iter().collect()
}

mod words;

#[cfg(all(test, unix))]
mod tests;
