//! Git fixture isolation shared by unit tests and feature-gated integration tests.
use std::{
    path::Path,
    process::{Command, Stdio},
};

/// Discard all caller Git controls, including hook redirects and config injection.
/// HOME and XDG paths belong to the fixture; neither host config nor hooks participate.
pub fn configure(command: &mut Command, root: &Path) {
    let mut keys: Vec<_> = std::env::vars_os().map(|(key, _)| key).collect();
    keys.extend(command.get_envs().map(|(key, _)| key.to_owned()));
    for key in keys {
        if key
            .to_string_lossy()
            .to_ascii_uppercase()
            .starts_with("GIT_")
        {
            command.env_remove(key);
        }
    }
    let null = if cfg!(windows) { "NUL" } else { "/dev/null" };
    command
        .current_dir(root)
        .stdin(Stdio::null())
        .env("HOME", root)
        .env("USERPROFILE", root)
        .env("XDG_CONFIG_HOME", root.join(".config"))
        .env("XDG_STATE_HOME", root.join(".local/state"))
        .env("XDG_CACHE_HOME", root.join(".cache"))
        .env("GIT_CONFIG_GLOBAL", null)
        .env("GIT_CONFIG_SYSTEM", null)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("GIT_AUTHOR_NAME", "tester")
        .env("GIT_AUTHOR_EMAIL", "t@example.com")
        .env("GIT_COMMITTER_NAME", "tester")
        .env("GIT_COMMITTER_EMAIL", "t@example.com");
}

pub fn command(root: &Path) -> Command {
    let mut command = Command::new("git");
    configure(&mut command, root);
    command
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fixture_overrides_caller_git_controls() {
        let root = tempfile::tempdir().unwrap();
        let mut cmd = Command::new("git");
        cmd.env("GIT_DIR", "/caller/repo")
            .env("GIT_CONFIG_COUNT", "1")
            .env("GIT_CONFIG_KEY_0", "core.hooksPath")
            .env("GIT_CONFIG_VALUE_0", "/caller/hooks");
        configure(&mut cmd, root.path());
        let vars: std::collections::BTreeMap<_, _> = cmd.get_envs().collect();
        for key in [
            "GIT_DIR",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_KEY_0",
            "GIT_CONFIG_VALUE_0",
        ] {
            assert_eq!(vars[std::ffi::OsStr::new(key)], None);
        }
        assert_eq!(cmd.get_current_dir(), Some(root.path()));
        assert_eq!(
            vars[std::ffi::OsStr::new("HOME")],
            Some(root.path().as_os_str())
        );
        assert_eq!(
            vars[std::ffi::OsStr::new("GIT_CONFIG_NOSYSTEM")],
            Some(std::ffi::OsStr::new("1"))
        );
    }

    #[test]
    fn fixture_git_can_commit_with_pinned_identity() {
        let root = tempfile::tempdir().unwrap();
        for args in [
            vec!["init", "-q"],
            vec!["commit", "-qm", "fixture", "--allow-empty"],
        ] {
            let out = command(root.path()).args(args).output().unwrap();
            assert!(
                out.status.success(),
                "{}",
                String::from_utf8_lossy(&out.stderr)
            );
        }
        let out = command(root.path())
            .args(["log", "-1", "--format=%an <%ae>"])
            .output()
            .unwrap();
        assert!(out.status.success());
        assert_eq!(
            String::from_utf8(out.stdout).unwrap().trim(),
            "tester <t@example.com>"
        );
    }
}
