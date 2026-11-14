use std::fs;
use std::process::{Command, Output};

fn cli(home: &std::path::Path, args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .env_clear()
        .env("HOME", home)
        .args(args)
        .output()
        .unwrap()
}

#[test]
fn setup_cli_uses_only_the_selected_temporary_home() {
    let root = tempfile::tempdir().unwrap();
    for args in [vec!["setup", "apply", "--dry-run"], vec!["setup", "check"]] {
        let output = cli(root.path(), &args);
        assert_eq!(
            output.status.code(),
            Some(if args[1] == "check" { 1 } else { 0 })
        );
        assert_eq!(fs::read_dir(root.path()).unwrap().count(), 0);
    }
    assert!(cli(root.path(), &["setup", "apply"]).status.success());
    assert!(cli(root.path(), &["setup", "check"]).status.success());
    let output = cli(root.path(), &["setup", "apply"]);
    assert!(output.status.success());
    assert!(String::from_utf8(output.stdout)
        .unwrap()
        .contains("skipped"));
    let values = fs::read_to_string(root.path().join(".config/plangraph/plangraph.env")).unwrap();
    assert!(values.contains("PG_LEASE_S=7200"));
}

#[test]
fn env_override_and_userprofile_fallback_refuse_credentials_without_echoing_them() {
    let root = tempfile::tempdir().unwrap();
    let env_path = root.path().join("selected.env");
    fs::write(&env_path, "GITHUB_PAT=sensitive-fixture\n").unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .env_clear()
        .env("USERPROFILE", root.path())
        .env("PLANGRAPH_ENV_FILE", env_path)
        .args(["env", "check"])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(stderr.contains("inline secret refused"));
    assert!(!stderr.contains("sensitive-fixture"));
}

#[test]
fn version_needs_no_home_and_later_slice_commands_are_not_dispatched() {
    let output = Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .env_clear()
        .arg("--version")
        .output()
        .unwrap();
    assert!(output.status.success());
    assert_eq!(
        String::from_utf8(output.stdout).unwrap(),
        "plangraph 0.1.0\n"
    );
    let root = tempfile::tempdir().unwrap();
    for command in [
        "unit-breaker",
        "taildrop-sort",
        "cost-gate",
        "namespace",
        "install",
        "migrate-breaker",
    ] {
        let output = cli(root.path(), &[command]);
        assert_eq!(output.status.code(), Some(2), "{command}");
        assert!(String::from_utf8(output.stderr).unwrap().contains("usage:"));
    }
    assert_eq!(fs::read_dir(root.path()).unwrap().count(), 0);
}
