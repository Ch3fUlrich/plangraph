use plangraph_cli::{
    breaker::{self, State},
    migrate,
};
use std::fs;
#[test]
fn five_identical_failures_disable_once_and_alert_once() {
    let root = tempfile::tempdir().unwrap();
    let alert = root.path().join("alert");
    let mut state = State::default();
    let mut calls = 0;
    for n in 0..5 {
        breaker::record(
            root.path(),
            "watch.service",
            5,
            &mut state,
            (1, &format!("pid {n} failed")),
            Some(&alert),
            || {
                calls += 1;
                true
            },
        )
        .unwrap();
    }
    assert!(state.tripped);
    assert_eq!(calls, 1);
    assert_eq!(fs::read_to_string(alert).unwrap().lines().count(), 1);
    assert_eq!(
        breaker::read(&root.path().join("watch.service.json"))
            .unwrap()
            .count,
        5
    );
}
#[test]
fn success_stop_different_signature_and_missing_manager() {
    let root = tempfile::tempdir().unwrap();
    let mut state = State::default();
    for (code, line) in [(1, "one"), (143, "neutral"), (1, "two"), (0, "ok")] {
        breaker::record(
            root.path(),
            "watch.service",
            5,
            &mut state,
            (code, line),
            None,
            || panic!("no trip"),
        )
        .unwrap();
        if code == 143 {
            assert_eq!(state.count, 1);
        }
        if line == "two" {
            assert_eq!(state.count, if cfg!(windows) { 2 } else { 1 });
        }
    }
    assert_eq!(state.count, 0);
    breaker::record(
        root.path(),
        "watch.service",
        1,
        &mut state,
        (3, "startup"),
        None,
        || false,
    )
    .unwrap();
    assert!(root.path().join("watch.service.tripped").exists());
}
#[test]
fn legacy_signature_and_copy_once_import() {
    assert_eq!(
        breaker::signature(1, "pid 42 died"),
        breaker::signature(1, "pid 99 died")
    );
    assert_eq!(
        breaker::signature(1, "crash 0xabcdef12"),
        breaker::signature(1, "crash 0x98765432")
    );
    let root = tempfile::tempdir().unwrap();
    let old = root.path().join("old");
    let new = root.path().join("new");
    fs::create_dir(&old).unwrap();
    fs::write(
        old.join("watch.service.json"),
        r#"{"count":4,"sig":"1:old","tripped":false}"#,
    )
    .unwrap();
    assert_eq!(migrate::breaker(&old, &new, true).unwrap(), 1);
    assert!(!new.exists());
    assert_eq!(migrate::breaker(&old, &new, false).unwrap(), 1);
    assert_eq!(migrate::breaker(&old, &new, false).unwrap(), 0);
    assert_eq!(
        breaker::read(&new.join("watch.service.json"))
            .unwrap()
            .count,
        4
    );
}
#[test]
fn refuses_bad_units_and_corrupt_state() {
    for unit in ["../x", "--help", "a/b", "a\\b", ".."] {
        assert!(!breaker::valid_unit(unit));
    }
    let root = tempfile::tempdir().unwrap();
    let file = root.path().join("bad.json");
    fs::write(&file, "{").unwrap();
    assert!(breaker::read(&file).is_err());
}
#[cfg(unix)]
#[test]
fn subprocess_refuses_sixth_command_and_status_reset_share_paths() {
    use std::os::unix::fs::PermissionsExt;
    use std::process::Command;
    let root = tempfile::tempdir().unwrap();
    let manager = root.path().join("systemctl");
    fs::write(
        &manager,
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$HOME/calls\"\ntest -s \"$HOME/alerts\"\n",
    )
    .unwrap();
    fs::set_permissions(&manager, fs::Permissions::from_mode(0o700)).unwrap();
    let invoke = |args: &[&str]| {
        Command::new(env!("CARGO_BIN_EXE_plangraph"))
            .args(args)
            .env("HOME", root.path())
            .env("XDG_STATE_HOME", root.path().join("state"))
            .env("PLANGRAPH_ENV_FILE", root.path().join("env"))
            .env("PLANGRAPH_ALERT_INBOX", root.path().join("alerts"))
            .env("PATH", root.path())
            .output()
            .unwrap()
    };
    for _ in 0..5 {
        let output = invoke(&[
            "unit-breaker",
            "--unit",
            "watch.service",
            "--",
            "/bin/sh",
            "-c",
            "echo failure >&2; exit 7",
        ]);
        assert_eq!(output.status.code(), Some(7));
    }
    assert_eq!(
        fs::read_to_string(root.path().join("calls")).unwrap(),
        "--user disable --now watch.service\n"
    );
    assert_eq!(
        fs::read_to_string(root.path().join("alerts"))
            .unwrap()
            .lines()
            .count(),
        1
    );
    assert_eq!(
        invoke(&[
            "unit-breaker",
            "--unit",
            "watch.service",
            "--",
            "/bin/sh",
            "-c",
            "touch marker"
        ])
        .status
        .code(),
        Some(1)
    );
    let status = invoke(&["unit-breaker", "status"]);
    assert!(String::from_utf8_lossy(&status.stdout).contains("state=tripped count=5"));
    assert!(invoke(&["unit-breaker", "reset", "watch.service"])
        .status
        .success());
    assert!(!root
        .path()
        .join("state/plangraph/breaker/watch.service.json")
        .exists());
}
#[test]
fn inaccessible_alert_still_disables_and_busy_import_refuses() {
    let root = tempfile::tempdir().unwrap();
    let mut state = State::default();
    let mut called = false;
    breaker::record(
        root.path(),
        "watch.service",
        1,
        &mut state,
        (1, "failure"),
        Some(&root.path().join("missing/alert")),
        || {
            called = true;
            true
        },
    )
    .unwrap();
    assert!(called);
    let old = root.path().join("old");
    let new = root.path().join("new");
    fs::create_dir(&old).unwrap();
    fs::create_dir(&new).unwrap();
    fs::write(old.join("watch.service.json"), "{}").unwrap();
    let _lock = plangraph_cli::atomic::lock(&new.join("watch.service.json")).unwrap();
    assert!(migrate::breaker(&old, &new, false).is_err());
    assert!(!new.join("watch.service.json").exists());
}
