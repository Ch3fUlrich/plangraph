//! A secret-free child environment for everything the worker spawns on an
//! agent's behalf: the harness and the card's check command.
//!
//! The worker process holds credentials (`PG_*`, `TEMPORAL_*`, keeper and TLS
//! settings, provider keys). A coding agent or a check script running in an
//! attempt worktree must not inherit them, so [`confine`] clears the child's
//! environment and re-adds only what [`PASS`] and [`PASS_PREFIXES`] name, plus a
//! private per-attempt `HOME` so tools cannot read the operator's dotfiles.

use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::Command;

/// Names copied from the worker's environment, when set.
///
/// `PATH`, `LANG` and `TERM` are what any tool needs to run at all.
/// `CARGO_HOME` and `RUSTUP_HOME` are toolchain *locations* (not secrets): with
/// `HOME` private, a Rust check could not find its toolchain without them.
pub const PASS: &[&str] = &["PATH", "LANG", "TERM", "CARGO_HOME", "RUSTUP_HOME"];

/// Name prefixes copied from the worker's environment: the agent CLIs' own
/// settings (`OPENCODE_*`, `CLAUDE_CODE_*`). Provider keys (`ANTHROPIC_*`,
/// `OPENAI_*`, ...) and the worker's own `PG_*` / `TEMPORAL_*` never match.
pub const PASS_PREFIXES: &[&str] = &["OPENCODE_", "CLAUDE_CODE_"];

/// True when `name` may reach a child from the worker's environment.
pub fn is_passed(name: &str) -> bool {
    PASS.contains(&name) || PASS_PREFIXES.iter().any(|p| name.starts_with(p))
}

/// Replace `cmd`'s environment with the allowlist, a private `HOME`, and the
/// variables the caller set on `cmd` explicitly (those are the harness's own by
/// construction, for example `OPENCODE_DISABLE_AUTOUPDATE` or a test's marker).
pub fn confine(cmd: &mut Command, home: &Path) {
    let explicit: Vec<(OsString, OsString)> = cmd
        .get_envs()
        .filter_map(|(k, v)| v.map(|v| (k.to_os_string(), v.to_os_string())))
        .collect();
    cmd.env_clear();
    for (k, v) in std::env::vars_os() {
        if k.to_str().is_some_and(is_passed) {
            cmd.env(k, v);
        }
    }
    for (k, v) in explicit {
        cmd.env(k, v);
    }
    cmd.env("HOME", home);
}

/// The private `HOME` of the attempt slot `dir`: a sibling named `<dir>.home`,
/// next to the worktree rather than in it so it never shows up as an
/// untracked change in the attempt's diff.
pub fn home_for(dir: &Path) -> PathBuf {
    let mut name = dir
        .file_name()
        .map(|n| n.to_os_string())
        .unwrap_or_else(|| OsString::from("attempt"));
    name.push(".home");
    dir.with_file_name(name)
}

/// Creates a private `HOME` and removes it on drop, unless it already existed
/// (an enclosing attempt owns it then).
pub struct HomeGuard {
    path: PathBuf,
    owned: bool,
}

impl HomeGuard {
    pub fn for_dir(dir: &Path) -> std::io::Result<HomeGuard> {
        let path = home_for(dir);
        let owned = !path.exists();
        std::fs::create_dir_all(&path)?;
        Ok(HomeGuard { path, owned })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for HomeGuard {
    fn drop(&mut self) {
        if self.owned {
            let _ = std::fs::remove_dir_all(&self.path);
        }
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    /// Run `env` through `confine` and return the child's variables.
    fn child_env(prep: impl FnOnce(&mut Command), home: &Path) -> Vec<String> {
        let mut c = Command::new("env");
        prep(&mut c);
        confine(&mut c, home);
        let out = c.output().expect("env runs");
        String::from_utf8_lossy(&out.stdout)
            .lines()
            .map(str::to_string)
            .collect()
    }

    #[test]
    fn allowlist_names_and_prefixes() {
        assert!(is_passed("PATH") && is_passed("LANG") && is_passed("TERM"));
        assert!(is_passed("OPENCODE_DISABLE_AUTOUPDATE") && is_passed("CLAUDE_CODE_OAUTH_TOKEN"));
        for n in [
            "PG_KEEPER_URL",
            "TEMPORAL_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "HOME",
        ] {
            assert!(!is_passed(n), "{n} must not pass");
        }
    }

    #[test]
    fn child_sees_allowlist_explicit_vars_and_private_home_only() {
        let home = tempfile::tempdir().unwrap();
        // A secret-looking variable in the worker's own environment. The name
        // is unique to this test, so no other test reads or sets it.
        std::env::set_var("PG_SENTINEL_SECRET_SANDBOX_ENV", "hunter2");
        let lines = child_env(
            |c| {
                c.env("PG_MARK", "explicit");
            },
            home.path(),
        );
        assert!(
            !lines
                .iter()
                .any(|l| l.contains("SENTINEL") || l.contains("hunter2")),
            "worker secret leaked into the child: {lines:?}"
        );
        assert!(lines.contains(&"PG_MARK=explicit".to_string()), "{lines:?}");
        assert!(
            lines.contains(&format!("HOME={}", home.path().display())),
            "{lines:?}"
        );
        assert!(lines.iter().any(|l| l.starts_with("PATH=")), "{lines:?}");
        // Nothing outside the allowlist survives: every name is passed,
        // explicit or HOME.
        for l in &lines {
            let name = l.split('=').next().unwrap();
            assert!(
                is_passed(name) || name == "HOME" || name == "PG_MARK",
                "unexpected {name} in child env"
            );
        }
    }

    #[test]
    fn home_guard_creates_and_removes_only_what_it_owns() {
        let root = tempfile::tempdir().unwrap();
        let dir = root.path().join("c1-1");
        assert_eq!(home_for(&dir), root.path().join("c1-1.home"));
        {
            let g = HomeGuard::for_dir(&dir).unwrap();
            assert!(g.path().is_dir());
            // A nested guard (a check inside an attempt) must not remove it.
            drop(HomeGuard::for_dir(&dir).unwrap());
            assert!(g.path().is_dir());
        }
        assert!(!home_for(&dir).exists());
    }
}
