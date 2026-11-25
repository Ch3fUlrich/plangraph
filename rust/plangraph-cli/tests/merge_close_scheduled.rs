//! The scheduled merge-close pass: a pending keeper refresh, its streak and the service units.
use plangraph_cli::merge_close::{run_pass, Pass, Refresh, ScheduleTiming};
use serde_json::json;
use std::time::Duration;

mod merge_close_support;
use merge_close_support::Script;

#[test]
fn a_refresh_running_at_the_wait_is_a_pending_pass_not_an_error() {
    let control = Script::new([
        (json!({"refresh":"resume"}), json!({"scan":"running"})),
        (json!({"refresh":"status"}), json!({"scan":"running"})),
    ]);
    let keeper = Script::new([]);
    let mut seen = Vec::new();
    let pass = run_pass(
        &keeper,
        &control,
        None,
        true,
        None,
        false,
        ScheduleTiming::new(Duration::ZERO, Duration::from_secs(400)).resuming(),
        &mut |refresh| {
            seen.push(format!("{refresh:?}"));
            Ok(())
        },
    )
    .unwrap();
    assert_eq!(pass, Pass::Pending { waited_secs: 0 });
    assert_eq!(seen, ["Pending { waited_secs: 0 }"]);
    control.assert_finished();
    keeper.assert_finished();
}

#[test]
fn a_failed_refresh_and_a_bad_status_stay_errors() {
    for (scan, expected) in [
        ("failed", "keeper merge index refresh failed"),
        ("weird", "keeper reply has no valid merge index status"),
    ] {
        let control = Script::new([
            (json!({"refresh":"start"}), json!({"scan":"running"})),
            (json!({"refresh":"status"}), json!({"scan":scan})),
        ]);
        let error = run_pass(
            &Script::new([]),
            &control,
            None,
            true,
            None,
            false,
            ScheduleTiming::new(Duration::ZERO, Duration::from_secs(400)),
            &mut |_| panic!("no refresh outcome"),
        )
        .unwrap_err();
        assert_eq!(error, expected);
    }
}

#[test]
fn only_the_first_refresh_of_a_pass_reaches_the_streak_hook() {
    let keeper = Script::new([
        (json!({"refresh":"start"}), json!({"scan":"running"})),
        (json!({"refresh":"status"}), json!({"scan":"ready"})),
        (json!({"refresh":"projects"}), json!({"projects":["demo"]})),
        (
            json!({"apply":true,"project":"demo"}),
            json!({"closed":[{"key":"demo:A"}],"merged_not_done":0,"next_after":"demo:A"}),
        ),
        (
            json!({"apply":true,"project":"demo","after":"demo:A"}),
            json!("merge_index_expired"),
        ),
        (json!({"refresh":"start"}), json!({"scan":"running"})),
        (json!({"refresh":"status"}), json!({"scan":"running"})),
    ]);
    let mut calls = 0;
    let pass = run_pass(
        &keeper,
        &keeper,
        None,
        true,
        None,
        false,
        ScheduleTiming::new(Duration::ZERO, Duration::from_secs(400)),
        &mut |refresh| {
            calls += 1;
            assert_eq!(*refresh, Refresh::Ready);
            Ok(())
        },
    )
    .unwrap();
    assert_eq!(calls, 1);
    let Pass::Done(done) = pass else {
        panic!("pending")
    };
    assert!(done.text.contains("closed\tdemo:A\t\n"));
    assert!(done.text.contains("continue\t1 projects remaining"));
    keeper.assert_finished();
}

#[cfg(unix)]
#[test]
fn scheduled_cli_pending_streak_survives_processes_and_resets() {
    use std::{fs, process::Command};

    let root = tempfile::tempdir().unwrap();
    let curl = root.path().join("curl");
    let token = root.path().join("token");
    let mode = root.path().join("mode");
    let log = root.path().join("log");
    let state = root.path().join("state").join("pending.json");
    fs::write(&token, "fixture").unwrap();
    fs::write(&log, "").unwrap();
    pg_worker_core::executable_test_support::write_executable(
        &curl,
        r#"#!/bin/sh
/bin/cat >/dev/null
printf '%s\n' "$*" >> "$LOG"
case "$*" in
  *'"refresh":"status"'*) printf '{"scan":"%s"}\n200' "$(/bin/cat "$MODE")" ;;
  *'"refresh":"start"'*|*'"refresh":"resume"'*) printf '{"scan":"running"}\n200' ;;
  *'"refresh":"projects"'*) printf '{"projects":["demo"]}\n200' ;;
  *) printf '{"closed":[{"key":"demo:A"}],"merged_not_done":0}\n200' ;;
esac
"#,
    );
    let run = |extra: &[&str]| {
        Command::new(env!("CARGO_BIN_EXE_plangraph"))
            .args(["merge-close", "--scheduled", "--state"])
            .arg(&state)
            .args(extra)
            .env_clear()
            .env("HOME", root.path())
            .env("PATH", root.path())
            .env("PG_URL", "http://127.0.0.1")
            .env("PG_TOKEN_FILE", &token)
            .env("PG_MERGE_CLOSE_REFRESH_WAIT_SECS", "0")
            .env("LOG", &log)
            .env("MODE", &mode)
            .output()
            .unwrap()
    };
    let closes = || {
        fs::read_to_string(&log)
            .unwrap()
            .lines()
            .filter(|line| line.contains("apply"))
            .count()
    };
    fs::write(&mode, "running").unwrap();
    for (expected, line) in [
        (0, "refresh_pending\t0\t1/3"),
        (0, "refresh_pending\t0\t2/3"),
        (1, "refresh_pending\t0\t3/3"),
    ] {
        let output = run(&[]);
        assert_eq!(output.status.code(), Some(expected), "{output:?}");
        assert!(String::from_utf8(output.stdout).unwrap().contains(line));
    }
    assert_eq!(closes(), 0);
    assert!(fs::read_to_string(&state)
        .unwrap()
        .contains("\"pending\":3"));
    // A ready pass continues the pending scan (resume), closes work and resets the streak.
    fs::write(&mode, "ready").unwrap();
    let output = run(&[]);
    assert_eq!(output.status.code(), Some(0), "{output:?}");
    assert!(String::from_utf8(output.stdout)
        .unwrap()
        .contains("closed\tdemo:A\t\n"));
    assert_eq!(closes(), 1);
    assert!(fs::read_to_string(&log)
        .unwrap()
        .contains("\"refresh\":\"resume\""));
    assert!(fs::read_to_string(&state)
        .unwrap()
        .contains("\"pending\":0"));
    // The count starts over after a reset.
    fs::write(&mode, "running").unwrap();
    let output = run(&[]);
    assert_eq!(output.status.code(), Some(0));
    assert!(String::from_utf8(output.stdout)
        .unwrap()
        .contains("refresh_pending\t0\t1/3"));
    // The continuation run reads the same state and resumes; with no pending pass it does nothing.
    fs::write(&mode, "ready").unwrap();
    let output = run(&["--continue"]);
    assert_eq!(output.status.code(), Some(0), "{output:?}");
    assert_eq!(closes(), 2);
    let before = fs::read_to_string(&log).unwrap();
    let output = run(&["--continue"]);
    assert_eq!(output.status.code(), Some(0));
    assert!(output.stdout.is_empty());
    assert_eq!(fs::read_to_string(&log).unwrap(), before);
}

#[test]
fn the_scheduled_mode_stays_out_of_count_and_preview_flags() {
    for args in [
        &["merge-close", "--scheduled"][..],
        &["merge-close", "--scheduled", "--state", "x", "--count"],
        &[
            "merge-close",
            "--scheduled",
            "--state",
            "x",
            "--deploy-status",
        ],
        &["merge-close", "--continue"],
        &["merge-close", "--count", "--state", "x"],
    ] {
        let output = std::process::Command::new(env!("CARGO_BIN_EXE_plangraph"))
            .args(args)
            .env_clear()
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(2), "{args:?}");
    }
}

#[test]
fn the_merge_close_service_runs_the_scheduled_mode_with_a_state_directory() {
    let service = include_str!("../../../deploy/systemd/pg-merge-close.service");
    let retry = include_str!("../../../deploy/systemd/pg-merge-close-continue.service");
    let timer = include_str!("../../../deploy/systemd/pg-merge-close-continue.timer");
    for unit in [service, retry] {
        assert!(unit.contains("StateDirectory=pg-merge-close\n"));
        assert!(unit.contains(" merge-close --scheduled --state %S/pg-merge-close/pending.json"));
    }
    assert!(retry.contains("--continue"));
    assert!(timer.contains("Unit=pg-merge-close-continue.service"));
    assert!(timer.contains("OnUnitActiveSec=1min"));
}

#[cfg(unix)]
#[test]
fn a_scheduled_run_skips_while_another_run_holds_the_state() {
    // A state file named like a lock must not be the lock: the streak write replaces its inode.
    for name in ["pending.json", "pending.lock"] {
        skips_while_held(name);
    }
}

#[cfg(unix)]
fn skips_while_held(name: &str) {
    use std::process::Command;

    let root = tempfile::tempdir().unwrap();
    let state = root.path().join("state").join(name);
    let run = || {
        Command::new(env!("CARGO_BIN_EXE_plangraph"))
            .args(["merge-close", "--scheduled", "--state"])
            .arg(&state)
            .env_clear()
            .env("HOME", root.path())
            .env("PATH", root.path()) // no curl: a run that is not skipped fails
            .env("PG_URL", "http://127.0.0.1")
            .env("PG_TOKEN_FILE", root.path().join("token"))
            .output()
            .unwrap()
    };
    // The first run creates the state directory and the lock, then fails on the missing token.
    assert_eq!(run().status.code(), Some(1));
    let held = pg_worker_core::lockfile::try_lock(
        &plangraph_cli::merge_close_unit::lock_path(&state).unwrap(),
    )
    .unwrap()
    .expect("lock free after the run");
    for extra in [false, true] {
        let mut command = Command::new(env!("CARGO_BIN_EXE_plangraph"));
        command
            .args(["merge-close", "--scheduled", "--state"])
            .arg(&state)
            .env_clear()
            .env("HOME", root.path())
            .env("PATH", root.path());
        if extra {
            command.arg("--continue");
        }
        let output = command.output().unwrap();
        assert_eq!(output.status.code(), Some(0), "{output:?}");
        assert!(String::from_utf8(output.stdout)
            .unwrap()
            .starts_with("merge_close_busy"));
        assert!(!state.exists(), "a skipped run writes no streak");
    }
    drop(held);
    assert_eq!(run().status.code(), Some(1), "the lock is released");
}

#[cfg(unix)]
#[test]
fn a_state_path_without_a_file_name_is_refused_before_any_keeper_request() {
    use std::{fs, process::Command};

    let root = tempfile::tempdir().unwrap();
    let curl = root.path().join("curl");
    let log = root.path().join("log");
    fs::write(root.path().join("token"), "fixture").unwrap();
    fs::write(&log, "").unwrap();
    pg_worker_core::executable_test_support::write_executable(
        &curl,
        "#!/bin/sh\n/bin/cat >/dev/null\nprintf '%s\\n' \"$*\" >> \"$LOG\"\nprintf '{\"scan\":\"ready\"}\\n200'\n",
    );
    for state in ["", "/", ".."] {
        for extra in [false, true] {
            let mut command = Command::new(env!("CARGO_BIN_EXE_plangraph"));
            command
                .args(["merge-close", "--scheduled", "--state", state])
                .current_dir(root.path())
                .env_clear()
                .env("HOME", root.path())
                .env("PATH", root.path())
                .env("PG_URL", "http://127.0.0.1")
                .env("PG_TOKEN_FILE", root.path().join("token"))
                .env("LOG", &log);
            if extra {
                command.arg("--continue");
            }
            let output = command.output().unwrap();
            assert_eq!(output.status.code(), Some(1), "{state:?} {output:?}");
            assert!(String::from_utf8_lossy(&output.stderr).contains("names no file"));
        }
    }
    assert_eq!(fs::read_to_string(&log).unwrap(), "", "no keeper request");
    assert!(!root.path().join(".lock").exists());
}
