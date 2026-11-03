//! Shared Python/Rust state location. Empty values count as unset (unlike Python).
use std::ffi::OsString;
use std::path::PathBuf;

pub(crate) fn resolve_state(
    flag: Option<&str>,
    env_lookup: impl Fn(&str) -> Option<OsString>,
) -> Result<PathBuf, String> {
    let env = |key| env_lookup(key).filter(|v| !v.is_empty()).map(PathBuf::from);
    if let Some(flag) = flag.filter(|v| !v.is_empty()) {
        return Ok(flag.into());
    }
    if let Some(state) = env("PLANGRAPH_SPAWN_STATE") {
        return Ok(state);
    }
    if let Some(state) = env("XDG_STATE_HOME") {
        return Ok(state.join("plangraph"));
    }
    let home_key = if cfg!(windows) { "USERPROFILE" } else { "HOME" };
    env(home_key)
        .map(|p| p.join(".local/state/plangraph"))
        .ok_or_else(|| "cannot resolve spawner state directory".into())
}
