mod lane_support;
use lane_support::{git, Fixture};
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
        pg_worker_core::executable_test_support::write_executable(&path, script);
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

fn lane_ref(name: &str, sha: &str) -> String {
    format!("refs/heads/lane/{name}:{sha}")
}

#[test]
fn lane_cleanup_deletes_finished_lanes_and_logs_shas() {
    let f = Fixture::new();
    let merged = f.lane("lane/merged", "main", "m.txt", "m\n");
    f.main_ff("lane/merged");
    let patch = f.lane("lane/patch", "main", "p.txt", "p\n");
    f.main_pick(&patch);
    let r1 = f.lane("lane/sup-r1", "main", "s.txt", "s1\n");
    let r2 = f.lane("lane/sup-r2", "lane/sup-r1", "s2.txt", "s2\n");
    let live = f.lane("lane/live", "main", "l.txt", "l\n");
    assert!(f
        .record_replacement("B1", &lane_ref("sup-r1", &r1), &lane_ref("sup-r2", &r2))
        .status
        .success());
    f.hook_on(true);
    let (code, text) = f.cleanup(&["--apply"]);
    assert_eq!(code, 0, "{text}");
    assert_eq!(f.heads(), ["live", "sup-r2"], "{text}");
    let restore = f.restore_text();
    for sha in [&merged, &patch, &r1] {
        assert!(
            restore.contains(sha.as_str()),
            "restore row for {sha}\n{restore}"
        );
        assert!(text.contains(sha.as_str()), "log line for {sha}\n{text}");
        // The pinned object restores the branch.
        git(
            &f.work,
            &[
                "rev-parse",
                "--verify",
                &format!("refs/plangraph/restore/{sha}"),
            ],
        );
    }
    assert!(text.contains(&live), "{text}");
    assert!(
        text.contains("reconcile heads=5 eligible=3 protected=0 deleted=3 absent=0"),
        "{text}"
    );
}

#[test]
fn lane_cleanup_preserves_live_and_unproven_heads() {
    let f = Fixture::new();
    // An older round with unique work: the later round does not carry it.
    let uniq1 = f.lane("lane/uniq-r1", "main", "u1.txt", "u1\n");
    let uniq2 = f.lane("lane/uniq-r2", "main", "u2.txt", "u2\n");
    let rec = f.record_replacement(
        "B2",
        &lane_ref("uniq-r1", &uniq1),
        &lane_ref("uniq-r2", &uniq2),
    );
    assert!(
        !rec.status.success(),
        "replacement without the old work must be refused"
    );
    // A merge commit hides a resolution: the patch id of its parts proves nothing.
    f.lane("lane/mergey", "main", "x.txt", "x\n");
    git(&f.work, &["checkout", "-q", "-B", "side", "main"]);
    std::fs::write(f.work.join("y.txt"), "y\n").unwrap();
    git(&f.work, &["add", "."]);
    git(&f.work, &["commit", "-m", "side"]);
    git(&f.work, &["merge", "--no-ff", "-m", "merge", "lane/mergey"]);
    git(
        &f.work,
        &["push", "-q", "origin", "HEAD:refs/heads/lane/mergey2"],
    );
    // Merged by ancestry, but a claim, an open PR head and an open child PR base protect them.
    let claimed = f.lane("lane/claimed", "main", "c.txt", "c\n");
    let opened = f.lane("lane/opened", "main", "o.txt", "o\n");
    let parent = f.lane("lane/parent", "main", "pa.txt", "pa\n");
    git(&f.work, &["checkout", "-q", "main"]);
    for l in ["lane/claimed", "lane/opened", "lane/parent"] {
        git(&f.work, &["merge", "--no-edit", l]);
    }
    git(&f.work, &["push", "-q", "origin", "main"]);
    f.set("claims.json", r#"{"paths":[],"branches":["lane/claimed"]}"#);
    f.set(
        "pulls.json",
        &format!(
            r#"[{{"number":5,"headRefName":"lane/opened","baseRefName":"main","headRefOid":"{opened}"}},
               {{"number":6,"headRefName":"lane/child","baseRefName":"lane/parent","headRefOid":"{}"}}]"#,
            "1".repeat(40)
        ),
    );
    // A moved head: the scope names the old SHA after the remote advanced.
    let moved_old = f.lane("lane/moving", "main", "mv.txt", "mv\n");
    f.main_ff("lane/moving");
    f.lane("lane/moving", "lane/moving", "mv2.txt", "mv2\n");
    f.hook_on(true);
    let (code, text) = f.cleanup(&["--apply"]);
    assert_eq!(code, 0, "{text}");
    for kept in [
        "uniq-r1", "uniq-r2", "mergey", "mergey2", "claimed", "opened", "parent", "moving",
    ] {
        assert!(
            f.heads().contains(&kept.to_string()),
            "{kept} deleted\n{text}"
        );
    }
    assert!(
        text.contains(&format!("lane/claimed {claimed}")) && text.contains("(claimed)"),
        "{text}"
    );
    assert!(text.contains("(open-pr)"), "{text}");
    let _ = parent;
    // The old SHA of the moved head is kept and reported; no restore row exists for it.
    let (_, scoped) = f.cleanup(&["--apply", "--refs", &lane_ref("moving", &moved_old)]);
    assert!(scoped.contains("absent-or-moved kept"), "{scoped}");
    assert!(f.heads().contains(&"moving".to_string()));
    assert!(!f.restore_text().contains(&moved_old));
}

#[test]
fn lane_cleanup_delete_needs_the_app_remote_and_unresolved_authority_keeps_everything() {
    let f = Fixture::new();
    let merged = f.lane("lane/merged", "main", "m.txt", "m\n");
    f.main_ff("lane/merged");
    // The hook refuses a plain delete from any caller.
    f.hook_on(false);
    let plain = std::process::Command::new("git")
        .current_dir(&f.work)
        .args(["push", "origin", ":refs/heads/lane/merged"])
        .output()
        .unwrap();
    assert!(!plain.status.success());
    // The sweep through a remote that is not marked as the App's is refused, with a retry code.
    let (code, text) = f.cleanup(&["--apply"]);
    assert_eq!(code, 3, "{text}");
    assert!(
        text.contains("refused") && f.heads() == ["merged"],
        "{text}"
    );
    // Unresolved claims keep every head, even with the App remote.
    f.hook_on(true);
    let out = f.cli(&[
        "lane-cleanup",
        "--repo",
        f.work.to_str().unwrap(),
        "--state",
        f.state.to_str().unwrap(),
        "--gh-repo",
        "fixture/origin",
        "--apply",
    ]);
    let text = String::from_utf8_lossy(&out.stdout);
    assert!(text.contains("protection unresolved"), "{text}");
    assert_eq!(f.heads(), ["merged"]);
    // The retry with resolved authority deletes it.
    let (code, text) = f.cleanup(&["--apply"]);
    assert_eq!(code, 0, "{text}");
    assert!(f.heads().is_empty() && text.contains(&merged), "{text}");
}

#[test]
fn probe_cleanup_closes_only_bound_terminal_runs() {
    let f = Fixture::new();
    let state = f.state.to_str().unwrap();
    let head = |c: &str| c.repeat(40);
    for (pr, run, c) in [(1, 11, "a"), (2, 12, "b"), (3, 13, "c")] {
        let out = f.cli(&[
            "lane-cleanup",
            "record-probe",
            "--state",
            state,
            "--gh-repo",
            "fixture/origin",
            "--pr",
            &pr.to_string(),
            "--head",
            &head(c),
            "--run-id",
            &run.to_string(),
        ]);
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
    }
    let pr = |h: &str| format!(r#"{{"state":"OPEN","headRefOid":"{h}"}}"#);
    let run = |status: &str, h: &str| format!(r#"{{"status":"{status}","headSha":"{h}"}}"#);
    f.set("pr-1.json", &pr(&head("a")));
    f.set("run-11.json", &run("completed", &head("a")));
    f.set("pr-2.json", &pr(&head("b")));
    f.set("run-12.json", &run("in_progress", &head("b")));
    f.set("pr-3.json", &pr(&head("d")));
    f.set("run-13.json", &run("completed", &head("c")));
    f.set("pr-4.json", &pr(&head("e")));
    let out = f.cli(&[
        "lane-cleanup",
        "close-probes",
        "--state",
        state,
        "--gh-repo",
        "fixture/origin",
        "--apply",
    ]);
    let text = String::from_utf8_lossy(&out.stdout).to_string();
    assert!(out.status.success(), "{text}");
    assert_eq!(
        fs::read_to_string(f.gh.join("closed.log")).unwrap(),
        "1\n",
        "{text}"
    );
    assert!(
        text.contains("#2") && text.contains("run-active") && text.contains("head-moved"),
        "{text}"
    );
    // A second run closes nothing again.
    f.cli(&[
        "lane-cleanup",
        "close-probes",
        "--state",
        state,
        "--gh-repo",
        "fixture/origin",
        "--apply",
    ]);
    assert_eq!(fs::read_to_string(f.gh.join("closed.log")).unwrap(), "1\n");
}
