use std::{path::Path, process::Command};

fn git(repo: &Path, args: &[&str]) {
    let mut cmd = Command::new("git");
    for (key, _) in std::env::vars_os() {
        if key.to_string_lossy().starts_with("GIT_") {
            cmd.env_remove(key);
        }
    }
    let out = cmd
        .current_dir(repo)
        .args(args)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
}
fn fixture() -> tempfile::TempDir {
    let tmp = tempfile::tempdir().unwrap();
    git(tmp.path(), &["init", "-q"]);
    git(tmp.path(), &["config", "user.name", "Allowed"]);
    git(
        tmp.path(),
        &["config", "user.email", "allowed@example.invalid"],
    );
    tmp
}
fn check(repo: &Path) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_pg-repo-sync"))
        .arg("--repo")
        .arg(repo)
        .args([
            "--check-config",
            "--host",
            "worker",
            "--allow-name",
            "Allowed",
            "--allow-email",
            "allowed@example.invalid",
        ])
        .output()
        .unwrap()
}
#[test]
fn clean_checkout_silent() {
    let tmp = fixture();
    let out = check(tmp.path());
    assert!(out.status.success());
    assert!(out.stdout.is_empty() && out.stderr.is_empty());
}
#[test]
fn bare_or_wrong_identity_has_one_alert_and_exit_one() {
    for (key, value) in [
        ("core.bare", "true"),
        ("user.name", "Fixture"),
        ("user.name", ""),
        ("user.name", " Allowed "),
        ("user.email", "wrong@example.invalid"),
    ] {
        let tmp = fixture();
        git(tmp.path(), &["config", key, value]);
        let out = check(tmp.path());
        assert_eq!(out.status.code(), Some(1));
        let text = String::from_utf8(out.stdout).unwrap();
        assert_eq!(text.lines().count(), 1);
        assert!(
            text.starts_with("ALERT host=worker ") && text.contains(key) && text.contains(value)
        );
        assert!(text.contains(&format!("{:?}", tmp.path().display().to_string())));
    }
}
#[test]
fn exported_git_dir_cannot_redirect_config_read() {
    let clean = fixture();
    let damaged = fixture();
    git(damaged.path(), &["config", "core.bare", "true"]);
    let out = Command::new(env!("CARGO_BIN_EXE_pg-repo-sync"))
        .arg("--repo")
        .arg(clean.path())
        .args([
            "--check-config",
            "--host",
            "worker",
            "--allow-name",
            "Allowed",
            "--allow-email",
            "allowed@example.invalid",
        ])
        .env("GIT_DIR", damaged.path().join(".git"))
        .env("GIT_CONFIG_COUNT", "garbage")
        .output()
        .unwrap();
    assert!(out.status.success());
    assert!(out.stdout.is_empty());
}
