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

#[test]
fn feeder_dry_run_error_and_missing_once_write_nothing() {
    let root = tempfile::tempdir().unwrap();
    let output = cli(root.path(), &["feed", "--dry-run"]);
    assert_eq!(output.status.code(), Some(2));
    assert!(String::from_utf8(output.stderr)
        .unwrap()
        .contains("requires --once"));
    let output = cli(root.path(), &["feed", "--once", "--dry-run"]);
    assert_eq!(output.status.code(), Some(2));
    let row: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(row["decision"], "refused");
    assert_eq!(row["dry_run"], true);
    assert_eq!(fs::read_dir(root.path()).unwrap().count(), 0);
}

#[cfg(unix)]
#[test]
fn keeper_restart_checks_the_candidate_schema_before_requesting_restart() {
    use std::os::unix::fs::PermissionsExt;

    let root = tempfile::tempdir().unwrap();
    let checkout = root.path().join("candidate");
    fs::create_dir_all(checkout.join("src/plangraph/keeper")).unwrap();
    fs::write(checkout.join("src/plangraph/keeper/cli.py"), "").unwrap();
    for (name, script) in [
        ("python3", "#!/bin/sh\nprintf '%s\\n' \"$PWD|$PYTHONPATH|$*\" >> \"$TRACE\"\nfor arg do\n  if [ \"$arg\" = \"$FAIL_STEP\" ]; then exit 4; fi\ndone\n"),
        ("systemctl", "#!/bin/sh\nif [ \"$2\" = show ]; then printf '%s\\n' \"$UNIT_CHECKOUT\"; else printf '%s\\n' \"$*\" >> \"$TRACE\"; fi\n"),
    ] {
        let path = root.path().join(name);
        fs::write(&path, script).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
    }
    let config = root.path().join("keeper.json");
    let dsn_file = root.path().join("migration.dsn");
    for path in [&config, &dsn_file] {
        fs::write(path, "fixture").unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
    }
    let trace = root.path().join("trace");
    let migrate = format!(
        "{}|{}|-m plangraph.keeper.cli --config {} admin migrate --dsn-file {}",
        checkout.display(),
        checkout.join("src").display(),
        config.display(),
        dsn_file.display()
    );
    let check = format!(
        "{}|{}|-m plangraph.keeper.cli --config {} admin check-schema",
        checkout.display(),
        checkout.join("src").display(),
        config.display()
    );
    for (failure, unit_checkout, expected) in [
        ("mismatch", root.path(), vec![]),
        ("migrate", checkout.as_path(), vec![migrate.as_str()]),
        (
            "check-schema",
            checkout.as_path(),
            vec![migrate.as_str(), check.as_str()],
        ),
        (
            "",
            checkout.as_path(),
            vec![
                migrate.as_str(),
                check.as_str(),
                "--user restart plangraph-keeper.service",
            ],
        ),
    ] {
        fs::write(&trace, "").unwrap();
        let output = Command::new(env!("CARGO_BIN_EXE_plangraph"))
            .env_clear()
            .env("PATH", root.path())
            .env("TRACE", &trace)
            .env("FAIL_STEP", failure)
            .env("UNIT_CHECKOUT", unit_checkout)
            .arg("keeper-restart")
            .arg(&checkout)
            .arg(&config)
            .arg(&dsn_file)
            .output()
            .unwrap();
        assert_eq!(
            fs::read_to_string(&trace)
                .unwrap()
                .lines()
                .collect::<Vec<_>>(),
            expected,
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert_eq!(output.status.success(), failure.is_empty());
    }
}
