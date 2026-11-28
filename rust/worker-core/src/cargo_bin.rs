//! The PATH a child that runs `cargo` needs on a rustup host.
use std::{
    env,
    ffi::{OsStr, OsString},
    path::{Path, PathBuf},
};

/// `path` with the rustup bin directory first: the first existing one of `$CARGO_HOME/bin` and
/// `<home>/.cargo/bin`. Non-login shells and services leave it off PATH, and only the rustup
/// proxy honours `rust-toolchain.toml`. Unchanged when that directory is already on `path` or
/// neither exists.
pub fn path_with_cargo(
    path: Option<&OsStr>,
    cargo_home: Option<&OsStr>,
    home: Option<&OsStr>,
) -> Option<OsString> {
    let mut dirs: Vec<PathBuf> = path
        .map(|p| env::split_paths(p).collect())
        .unwrap_or_default();
    let candidates = [
        cargo_home
            .filter(|v| !v.is_empty())
            .map(|v| Path::new(v).join("bin")),
        home.filter(|v| !v.is_empty())
            .map(|v| Path::new(v).join(".cargo/bin")),
    ];
    let Some(bin) = candidates.into_iter().flatten().find(|dir| dir.is_dir()) else {
        return path.map(OsStr::to_os_string);
    };
    if dirs.contains(&bin) {
        return path.map(OsStr::to_os_string);
    }
    dirs.insert(0, bin);
    env::join_paths(dirs)
        .ok()
        .or_else(|| path.map(OsStr::to_os_string))
}

/// `path_with_cargo` for this process's `PATH`, `CARGO_HOME` and `HOME`.
pub fn child_path() -> Option<OsString> {
    path_with_cargo(
        env::var_os("PATH").as_deref(),
        env::var_os("CARGO_HOME").as_deref(),
        env::var_os("HOME").as_deref(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn os(value: &Path) -> OsString {
        value.as_os_str().to_os_string()
    }

    #[test]
    fn rustup_bin_goes_first_when_path_lacks_it() {
        let tmp = tempfile::tempdir().unwrap();
        let home = tmp.path().join("home");
        std::fs::create_dir_all(home.join(".cargo/bin")).unwrap();
        let path = OsString::from("/usr/bin:/bin");
        let found = path_with_cargo(Some(&path), None, Some(&os(&home))).unwrap();
        let dirs: Vec<PathBuf> = env::split_paths(&found).collect();
        assert_eq!(dirs[0], home.join(".cargo/bin"));
        assert_eq!(
            &dirs[1..],
            [PathBuf::from("/usr/bin"), PathBuf::from("/bin")]
        );
        // Already on PATH: unchanged, so repeated calls do not grow it.
        assert_eq!(
            path_with_cargo(Some(&found), None, Some(&os(&home))),
            Some(found)
        );
    }

    #[test]
    fn cargo_home_wins_over_the_home_default() {
        let tmp = tempfile::tempdir().unwrap();
        let cargo_home = tmp.path().join("cargo");
        let home = tmp.path().join("home");
        std::fs::create_dir_all(cargo_home.join("bin")).unwrap();
        std::fs::create_dir_all(home.join(".cargo/bin")).unwrap();
        let found = path_with_cargo(None, Some(&os(&cargo_home)), Some(&os(&home))).unwrap();
        assert_eq!(found, os(&cargo_home.join("bin")));
    }

    #[test]
    fn missing_rustup_dirs_leave_path_alone() {
        let tmp = tempfile::tempdir().unwrap();
        let path = OsString::from("/usr/bin");
        let unset = os(&tmp.path().join("absent"));
        assert_eq!(
            path_with_cargo(Some(&path), Some(&unset), Some(&os(tmp.path()))),
            Some(path)
        );
        assert_eq!(path_with_cargo(None, None, None), None);
    }
}
