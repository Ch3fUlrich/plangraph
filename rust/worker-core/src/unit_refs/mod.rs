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
    collect(dir, None, home, 0, &mut Vec::new(), &mut refs)?;
    Ok(refs.into_iter().collect())
}

fn collect(
    dir: &Path,
    owner: Option<&str>,
    home: &Path,
    depth: usize,
    active: &mut Vec<PathBuf>,
    refs: &mut BTreeSet<UnitRef>,
) -> io::Result<()> {
    // A directory already being read further up this traversal is a link cycle; one reached again
    // by another unit's link is read again, so each unit keeps its own attribution.
    let physical = dir.canonicalize().ok();
    if let Some(canonical) = &physical {
        if active.contains(canonical) {
            return Ok(());
        }
        active.push(canonical.clone());
    }
    let entries = match fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(e) => return Err(e),
    };
    for entry in entries {
        let entry = entry?;
        let path = entry.path();
        let name = entry.file_name().to_string_lossy().into_owned();
        let unit = owner.unwrap_or(&name).to_string();
        if fs::symlink_metadata(&path)?.file_type().is_symlink() {
            // A linked unit: the link target is itself a path the unit runs from. A relative
            // target resolves against the link's directory.
            if let Ok(target) = fs::read_link(&path) {
                // A relative target resolves against the link's physical directory (the canonical
                // `dir`: `dir` itself may be reached through a linked directory).
                let base = physical.clone().unwrap_or_else(|| dir.to_path_buf());
                let joined = if target.is_absolute() {
                    target
                } else {
                    base.join(target)
                };
                // Symlinks in the directory part resolve physically (`alias/..` is the alias
                // target's parent); the final name stays as named. A dangling directory part
                // falls back to lexical `..` handling.
                let lexical_path = lexical(&joined);
                let named = match (joined.parent().map(Path::canonicalize), joined.file_name()) {
                    (Some(Ok(parent)), Some(file)) => parent.join(file),
                    _ => lexical_path.clone(),
                };
                let exact = words::is_whole_word_path(&named.to_string_lossy());
                // Guesses that protect too: the lexical reading, and where a final link leads.
                let mut guesses = vec![lexical_path];
                guesses.extend(named.canonicalize().ok());
                for guess in guesses.into_iter().filter(|g| *g != named) {
                    refs.insert(UnitRef {
                        unit: unit.clone(),
                        path: guess,
                        exact: false,
                    });
                }
                refs.insert(UnitRef {
                    unit: unit.clone(),
                    path: named,
                    exact,
                });
            }
        }
        // Metadata follows links: a linked directory (`app.service.d`) is read like a real one.
        let Ok(meta) = fs::metadata(&path) else {
            continue;
        };
        if meta.is_dir() {
            if depth >= 4 {
                return Err(io::Error::other("unit directory nesting too deep"));
            }
            let owner = name
                .strip_suffix(".d")
                .filter(|n| n.contains('.'))
                .or(owner);
            collect(&path, owner, home, depth + 1, active, refs)?;
        } else if meta.is_file() {
            let bytes = fs::read(&path)?;
            if bytes.len() > 1 << 20 {
                return Err(io::Error::other("unit file too large"));
            }
            for (path, exact) in words::text_paths(&String::from_utf8_lossy(&bytes), home) {
                refs.insert(UnitRef {
                    unit: unit.clone(),
                    path,
                    exact,
                });
            }
        }
    }
    if physical.is_some() {
        active.pop();
    }
    Ok(())
}

/// `.` and `..` resolved without touching the file system (a dangling link has no canonical form).
fn lexical(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for part in path.components() {
        match part {
            std::path::Component::ParentDir => {
                out.pop();
            }
            std::path::Component::CurDir => {}
            other => out.push(other),
        }
    }
    out
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
        .filter(|r| {
            // Only "not found" is missing: a permission error says nothing about the worktree.
            r.exact
                && worktree_root(&r.path).is_some_and(|root| {
                    matches!(fs::metadata(&root), Err(e) if e.kind() == io::ErrorKind::NotFound)
                })
        })
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
