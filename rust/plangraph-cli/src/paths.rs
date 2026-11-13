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
