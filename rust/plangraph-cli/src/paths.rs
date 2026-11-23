use std::path::{Path, PathBuf};
pub fn state(home: &Path) -> PathBuf {
    std::env::var_os("XDG_STATE_HOME")
        .map(PathBuf::from)
        .or_else(|| {
            if cfg!(windows) {
                std::env::var_os("LOCALAPPDATA").map(PathBuf::from)
            } else {
                None
            }
        })
        .unwrap_or_else(|| home.join(".local/state"))
        .join("plangraph")
}

pub(crate) fn repo_relative(path: &str) -> &str {
    path.trim_start_matches("./").trim_end_matches('/')
}

/// Compare files and module directories on slash-delimited repository path boundaries.
pub(crate) fn paths_overlap(a: &str, b: &str) -> bool {
    let (a, b) = (repo_relative(a), repo_relative(b));
    a == b
        || a.strip_prefix(b).is_some_and(|rest| rest.starts_with('/'))
        || b.strip_prefix(a).is_some_and(|rest| rest.starts_with('/'))
}
