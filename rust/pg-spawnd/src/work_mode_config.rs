//! Work-mode configuration: repository, user, then batch.

use pg_worker_core::work_mode::WorkMode;
use std::{io::ErrorKind, path::Path};

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct FileConfig {
    work_mode: WorkMode,
}

/// Load a setting without resolving Auto or starting any orchestration.
/// `repo_root` is the caller's repository root, not the process working directory.
pub(crate) fn configured_mode(repo_root: &Path) -> Result<WorkMode, String> {
    load_from(repo_root, &crate::prompt::config_root())
}

// Inject paths for deterministic tests without mutating the process environment.
fn load_from(repo_root: &Path, user_config_root: &Path) -> Result<WorkMode, String> {
    load_paths(&[
        repo_root.join(".plangraph/work-mode.json"),
        user_config_root.join("work-mode.json"),
    ])
}

pub(crate) fn user_mode() -> Result<WorkMode, String> {
    load_paths(&[crate::prompt::config_root().join("work-mode.json")])
}

fn load_paths(paths: &[std::path::PathBuf]) -> Result<WorkMode, String> {
    for path in paths {
        let bytes = match std::fs::read(path) {
            Ok(bytes) => bytes,
            Err(error) if error.kind() == ErrorKind::NotFound => continue,
            Err(error) => return Err(format!("work mode config {}: {error}", path.display())),
        };
        // Serde structs also accept positional arrays; the config schema requires an object.
        if bytes.iter().find(|byte| !byte.is_ascii_whitespace()) != Some(&b'{') {
            return Err(format!(
                "work mode config {}: expected a JSON object",
                path.display()
            ));
        }
        return serde_json::from_slice::<FileConfig>(&bytes)
            .map(|config| config.work_mode)
            .map_err(|error| format!("work mode config {}: {error}", path.display()));
    }
    Ok(WorkMode::default())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicUsize, Ordering};

    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            static NEXT: AtomicUsize = AtomicUsize::new(0);
            let root = std::env::temp_dir().join(format!(
                "pg-work-mode-{}-{}-{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_nanos(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::create_dir_all(root.join("repo/.plangraph")).unwrap();
            std::fs::create_dir_all(root.join("user")).unwrap();
            Self(root)
        }
        fn repo(&self) -> PathBuf {
            self.0.join("repo")
        }
        fn user(&self) -> PathBuf {
            self.0.join("user")
        }
        fn write(&self, rel: &str, json: &str) {
            std::fs::write(self.0.join(rel), json).unwrap();
        }
        fn load(&self) -> Result<WorkMode, String> {
            load_from(&self.repo(), &self.user())
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn precedence_repo_user_default() {
        let f = Fixture::new();
        assert_eq!(f.load().unwrap(), WorkMode::Batch);
        f.write("user/work-mode.json", r#"{"work_mode":"piecewise"}"#);
        assert_eq!(f.load().unwrap(), WorkMode::Piecewise);
        f.write("repo/.plangraph/work-mode.json", r#"{"work_mode":"auto"}"#);
        assert_eq!(f.load().unwrap(), WorkMode::Auto);
        // A valid repository override need not read the lower-priority file.
        f.write("user/work-mode.json", "invalid JSON");
        assert_eq!(f.load().unwrap(), WorkMode::Auto);
    }

    #[test]
    fn invalid_selected_config_never_falls_back() {
        let f = Fixture::new();
        for rel in ["user/work-mode.json", "repo/.plangraph/work-mode.json"] {
            for json in [
                r#"{"work_mode":"future"}"#,
                r#"{"work_mode":null}"#,
                r#"{"work_mode":1}"#,
                r#"{"work_mode":true}"#,
                r#"{"work_mode":{"auto":null}}"#,
                r#"{"work_mode":"batch","typo":true}"#,
                "{}",
                "[]",
                r#"["batch"]"#,
                "null",
                "bad JSON",
                r#"{"work_mode":"batch","work_mode":"auto"}"#,
            ] {
                f.write(rel, json);
                let error = f.load().unwrap_err();
                assert!(error.contains("work-mode.json"), "{error}");
            }
            std::fs::remove_file(f.0.join(rel)).unwrap();
        }
    }

    #[test]
    fn configured_mode_uses_shared_xdg_and_home_lookup() {
        const CHILD: &str = "PG_WORK_MODE_CONFIG_TEST_ROOT";
        if let Some(root) = std::env::var_os(CHILD) {
            assert_eq!(
                configured_mode(&PathBuf::from(root).join("repo")).unwrap(),
                if std::env::var_os("XDG_CONFIG_HOME").is_some() {
                    WorkMode::Auto
                } else {
                    WorkMode::Piecewise
                }
            );
            return;
        }
        let f = Fixture::new();
        for rel in [
            "xdg/plangraph",
            "home/.config/plangraph",
            "appdata/plangraph",
        ] {
            std::fs::create_dir_all(f.0.join(rel)).unwrap();
            f.write(
                &format!("{rel}/work-mode.json"),
                if rel.starts_with("xdg/") {
                    r#"{"work_mode":"auto"}"#
                } else {
                    r#"{"work_mode":"piecewise"}"#
                },
            );
        }
        for xdg in [true, false] {
            let mut cmd = std::process::Command::new(std::env::current_exe().unwrap());
            cmd.args([
                "--exact",
                "work_mode_config::tests::configured_mode_uses_shared_xdg_and_home_lookup",
            ])
            .env(CHILD, &f.0)
            .env("HOME", f.0.join("home"))
            .env("APPDATA", f.0.join("appdata"))
            .env_remove("XDG_CONFIG_HOME");
            if xdg {
                cmd.env("XDG_CONFIG_HOME", f.0.join("xdg"));
            }
            let output = cmd.output().unwrap();
            assert!(
                output.status.success(),
                "{}",
                String::from_utf8_lossy(&output.stdout)
            );
        }
    }

    #[test]
    fn read_errors_are_not_missing_config() {
        let f = Fixture::new();
        std::fs::create_dir(f.user().join("work-mode.json")).unwrap();
        assert!(f.load().unwrap_err().contains("work-mode.json"));
        std::fs::remove_dir(f.user().join("work-mode.json")).unwrap();
        std::fs::create_dir(f.repo().join(".plangraph/work-mode.json")).unwrap();
        assert!(f.load().unwrap_err().contains("work-mode.json"));
    }
}
