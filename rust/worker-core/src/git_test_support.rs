//! Git fixture isolation shared by unit tests and feature-gated integration tests.
use std::{
    path::Path,
    process::{Command, Stdio},
};

const GIT_ENTRY: &str = ".git";

/// Discard all caller Git controls, including hook redirects and config injection.
/// HOME and XDG paths belong to the fixture; neither host config nor hooks participate.
pub fn configure(command: &mut Command, root: &Path) {
    assert!(
        common_dir(root)
            .is_none_or(|dir| Some(dir) != common_dir(Path::new(env!("CARGO_MANIFEST_DIR")))),
        "Git fixture must not operate on the source repository or its worktrees"
    );
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

// Read metadata directly: invoking Git here would itself inherit hook redirects.
fn common_dir(root: &Path) -> Option<std::path::PathBuf> {
    let root = root.canonicalize().ok()?;
    for ancestor in root.ancestors() {
        let entry = ancestor.join(GIT_ENTRY);
        let metadata = if entry.is_dir() {
            entry
        } else if entry.is_file() {
            ancestor.join(
                std::fs::read_to_string(entry)
                    .ok()?
                    .strip_prefix("gitdir:")?
                    .trim(),
            )
        } else {
            continue;
        };
        let common = metadata.join("commondir");
        return if common.is_file() {
            metadata
                .join(std::fs::read_to_string(common).ok()?.trim())
                .canonicalize()
                .ok()
        } else {
            metadata.canonicalize().ok()
        };
    }
    None
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
    #[should_panic(expected = "Git fixture must not operate on the source repository")]
    fn fixture_refuses_source_repository() {
        command(Path::new(env!("CARGO_MANIFEST_DIR")));
    }

    #[test]
    #[should_panic(expected = "Git fixture must not operate on the source repository")]
    fn fixture_refuses_linked_source_repository() {
        let root = tempfile::tempdir().unwrap();
        let metadata = root.path().join("metadata");
        std::fs::create_dir(&metadata).unwrap();
        std::fs::write(root.path().join(GIT_ENTRY), "gitdir: metadata\n").unwrap();
        let source = common_dir(Path::new(env!("CARGO_MANIFEST_DIR"))).unwrap();
        std::fs::write(metadata.join("commondir"), source.to_str().unwrap()).unwrap();
        command(root.path());
    }

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
