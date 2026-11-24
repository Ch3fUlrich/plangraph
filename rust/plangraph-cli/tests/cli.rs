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

#[cfg(all(target_os = "linux", target_arch = "x86_64"))]
#[test]
fn setup_sccache_plans_the_pinned_executable_without_writing_home() {
    let root = tempfile::tempdir().unwrap();
    let args = ["setup", "apply", "--component", "sccache", "--dry-run"];
    let output = cli(root.path(), &args);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        String::from_utf8(output.stdout).unwrap(),
        "sccache .local/bin/sccache installed manifest\n"
    );
    assert_eq!(fs::read_dir(root.path()).unwrap().count(), 0);
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
    let interpreter = checkout.join("venv/bin/keeper-python");
    fs::create_dir_all(interpreter.parent().unwrap()).unwrap();
    for (path, script) in [
        (root.path().join("python3"), "#!/bin/sh\nexit 97\n"),
        (
            interpreter.clone(),
            r#"#!/bin/sh
printf '%s\n' "$0|$UNIT_SETTING|$*" >> "$TRACE"
if [ "$FAIL_STEP" = migrate ] && [ "${*##* }" = --dry-run ]; then exit 0; fi
for arg do
  if [ "$arg" = "$FAIL_STEP" ]; then exit 4; fi
done
"#,
        ),
        (
            root.path().join("systemctl"),
            r#"#!/bin/sh
if [ "$2" = show ]; then printf '%s\n' "$UNIT_CHECKOUT"; exit; fi
printf '%s\n' "$*" >> "$TRACE"
[ "$2" != "$FAIL_STEP" ]
"#,
        ),
        (
            root.path().join("busctl"),
            r#"#!/bin/sh
case "$3" in
  call) printf '%s\n' inspect >> "$TRACE"; /bin/cat "$FIXTURE/unit" ;;
  get-property) shift 6; for property do /bin/cat "$FIXTURE/$property"; done ;;
  *) exit 99 ;;
esac
"#,
        ),
        (
            root.path().join("systemd-run"),
            r#"#!/bin/sh
while [ "$1" != -- ]; do
  case "$1" in
    --property=EnvironmentFile=-*) [ "${1#*=EnvironmentFile=-}" = "$EXTRA_ENV" ] || exit 9; extra=yes ;;
    --property=EnvironmentFile=*) /bin/grep -Fx 'UNIT_SETTING="configured value"' "${1#*=EnvironmentFile=}" >/dev/null || exit 8; direct=yes ;;
    '--property=UnsetEnvironment="STALE"') unset_arg=yes ;;
  esac
  shift
done
[ "$direct:$extra:$unset_arg" = yes:yes:yes ] || exit 7
shift
UNIT_SETTING='configured value' exec "$@"
"#,
        ),
    ] {
        fs::write(&path, script).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
    }
    let config = root.path().join("keeper.json");
    let dsn_file = root.path().join("migration.dsn");
    for path in [&config, &dsn_file] {
        fs::write(path, "fixture").unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
    }
    let extra_env = root.path().join("runtime environment");
    for (file, data) in [
        (
            "unit",
            serde_json::json!(["/org/freedesktop/systemd1/unit/keeper"]),
        ),
        (
            "ExecStartEx",
            serde_json::json!([[
                interpreter,
                [
                    interpreter.to_str().unwrap(),
                    "-m",
                    "plangraph.keeper.daemon",
                    "--config",
                    config.to_str().unwrap()
                ],
                [],
                0,
                0,
                0,
                0,
                0,
                0,
                0
            ]]),
        ),
        (
            "Environment",
            serde_json::json!(["UNIT_SETTING=configured value"]),
        ),
        ("EnvironmentFiles", serde_json::json!([[extra_env, true]])),
        ("UnsetEnvironment", serde_json::json!(["STALE"])),
    ] {
        fs::write(
            root.path().join(file),
            format!("{}\n", serde_json::json!({"data": data})),
        )
        .unwrap();
    }
    let trace = root.path().join("trace");
    let migrate = format!(
        "{}|configured value|-m plangraph.keeper.cli --config {} admin migrate --dsn-file {}",
        interpreter.display(),
        config.display(),
        dsn_file.display()
    );
    let dry_check = format!("{migrate} --dry-run");
    let check = format!(
        "{}|configured value|-m plangraph.keeper.cli --config {} admin check-schema",
        interpreter.display(),
        config.display()
    );
    let reload = "--user daemon-reload";
    for (failure, unit_checkout, expected) in [
        ("daemon-reload", checkout.as_path(), vec![reload]),
        ("mismatch", root.path(), vec![reload, "inspect"]),
        (
            "--dry-run",
            checkout.as_path(),
            vec![reload, "inspect", dry_check.as_str()],
        ),
        (
            "migrate",
            checkout.as_path(),
            vec![reload, "inspect", dry_check.as_str(), migrate.as_str()],
        ),
        (
            "check-schema",
            checkout.as_path(),
            vec![
                reload,
                "inspect",
                dry_check.as_str(),
                migrate.as_str(),
                check.as_str(),
            ],
        ),
        (
            "",
            checkout.as_path(),
            vec![
                reload,
                "inspect",
                dry_check.as_str(),
                migrate.as_str(),
                check.as_str(),
                "--user restart plangraph-keeper.service",
            ],
        ),
    ] {
        fs::write(
            root.path().join("WorkingDirectory"),
            format!("{}\n", serde_json::json!({"data": unit_checkout})),
        )
        .unwrap();
        fs::write(&trace, "").unwrap();
        let output = Command::new(env!("CARGO_BIN_EXE_plangraph"))
            .env_clear()
            .env("PATH", root.path())
            .env("TRACE", &trace)
            .env("FAIL_STEP", failure)
            .env("UNIT_CHECKOUT", unit_checkout)
            .env("FIXTURE", root.path())
            .env("EXTRA_ENV", &extra_env)
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

#[cfg(unix)]
#[test]
fn baseline_runs_the_sibling_pg_telemetry_before_the_one_on_path() {
    use std::os::unix::fs::symlink;
    use std::path::Path;

    let program = |name: &str| {
        ["/usr/bin", "/bin"]
            .map(|d| Path::new(d).join(name))
            .into_iter()
            .find(|p| p.exists())
            .unwrap()
    };
    let root = tempfile::tempdir().unwrap();
    let install = root.path().join("install");
    let on_path = root.path().join("path");
    fs::create_dir_all(&install).unwrap();
    fs::create_dir_all(&on_path).unwrap();
    // A copy of plangraph, so that "next to the running binary" is a directory the test owns.
    let plangraph = install.join("plangraph");
    fs::copy(env!("CARGO_BIN_EXE_plangraph"), &plangraph).unwrap();
    let baseline = |args: &[&str]| {
        Command::new(&plangraph)
            .env_clear()
            .env("HOME", root.path())
            .env("PATH", &on_path)
            .arg("baseline")
            .args(args)
            .output()
            .unwrap()
    };
    // Neither a sibling nor a PATH entry: the fixed refusal.
    let output = baseline(&["--keeper-only"]);
    assert_eq!(output.status.code(), Some(1));
    assert!(String::from_utf8(output.stderr)
        .unwrap()
        .starts_with("baseline: cannot start pg-telemetry:"));
    // PATH only: it runs, and its exit code is returned.
    symlink(program("true"), on_path.join("pg-telemetry")).unwrap();
    assert_eq!(baseline(&["--keeper-only"]).status.code(), Some(0));
    // A sibling wins over PATH.
    symlink(program("false"), install.join("pg-telemetry")).unwrap();
    assert_eq!(baseline(&["--keeper-only"]).status.code(), Some(1));
}

#[test]
fn the_usage_line_names_baseline() {
    let root = tempfile::tempdir().unwrap();
    let output = cli(root.path(), &["no-such-command"]);
    assert!(String::from_utf8(output.stderr)
        .unwrap()
        .contains("baseline --keeper-only [--since RFC3339]"));
}
