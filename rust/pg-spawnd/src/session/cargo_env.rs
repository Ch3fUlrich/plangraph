//! Cargo target environment of a launched session: the host's shared target, never a checkout's.
use std::{
    ffi::{OsStr, OsString},
    path::Path,
};

/// `CARGO_TARGET_DIR` (and `PLANGRAPH_CARGO_TARGET_DIR`, for children that resolve it themselves)
/// of a session started here. `PLANGRAPH_CARGO_TARGET_DIR` in the host environment names the
/// host's target, for example on its data drive; without it the default shared target applies. A
/// `CARGO_TARGET_DIR` already set by the caller is kept.
pub(super) fn target_env(
    inherited: Option<&OsStr>,
    configured: Option<&OsStr>,
    home: &Path,
    cwd: &Path,
) -> Result<Vec<(&'static str, OsString)>, String> {
    if inherited.is_some_and(|value| !value.is_empty()) {
        return Ok(vec![]);
    }
    let target = pg_worker_core::cargo_cache::shared_target_from(configured, home, cwd)
        .map_err(|error| format!("shared cargo target: {error}"))?;
    Ok(vec![
        ("CARGO_TARGET_DIR", target.clone().into_os_string()),
        ("PLANGRAPH_CARGO_TARGET_DIR", target.into_os_string()),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pairs(env: Vec<(&'static str, OsString)>) -> Vec<(&'static str, String)> {
        env.into_iter()
            .map(|(k, v)| (k, v.to_string_lossy().into_owned()))
            .collect()
    }

    #[test]
    fn a_configured_data_drive_target_is_what_the_session_builds_into() {
        let home = Path::new("/home/worker");
        let cwd = Path::new("/work/tree");
        let env = target_env(None, Some(OsStr::new("/mnt/data/cargo-target")), home, cwd).unwrap();
        assert_eq!(
            pairs(env),
            [
                ("CARGO_TARGET_DIR", "/mnt/data/cargo-target".into()),
                (
                    "PLANGRAPH_CARGO_TARGET_DIR",
                    "/mnt/data/cargo-target".into()
                )
            ]
        );
        let home_relative =
            target_env(None, Some(OsStr::new("~/cache/target")), home, cwd).unwrap();
        assert_eq!(pairs(home_relative)[0].1, "/home/worker/cache/target");
    }

    #[test]
    fn the_default_is_the_shared_target_and_a_caller_setting_wins() {
        let home = Path::new("/home/worker");
        let cwd = Path::new("/work/tree");
        let env = target_env(None, None, home, cwd).unwrap();
        assert_eq!(
            pairs(env)[0].1,
            "/home/worker/.cache/plangraph/cargo-target"
        );
        let kept = target_env(
            Some(OsStr::new("/already/set")),
            Some(OsStr::new("/mnt/data/cargo-target")),
            home,
            cwd,
        )
        .unwrap();
        assert!(kept.is_empty());
        assert_eq!(
            target_env(Some(OsStr::new("")), None, home, cwd)
                .unwrap()
                .len(),
            2
        );
    }
}
