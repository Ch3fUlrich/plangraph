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
    fn configure_removes_explicit_redirects_and_config_injection() {
        let fixture = tempfile::tempdir().unwrap();
        let mut git = Command::new("git");
        git.env("GIT_DIR", fixture.path().join("missing-repository"))
            .env("GIT_CONFIG_COUNT", "1")
            .env("GIT_CONFIG_KEY_0", "user.name")
            .env("GIT_CONFIG_VALUE_0", "hostile");
        configure(&mut git, fixture.path());
        let output = git.args(["config", "--get", "user.name"]).output().unwrap();
        assert_eq!(output.status.code(), Some(1));
        assert!(output.stdout.is_empty());
        for key in [
            "GIT_DIR",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_KEY_0",
            "GIT_CONFIG_VALUE_0",
        ] {
            assert!(git
                .get_envs()
                .any(|(name, value)| name == key && value.is_none()));
        }
    }
}
