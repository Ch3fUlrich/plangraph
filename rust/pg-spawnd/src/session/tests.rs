use super::*;

fn args(extra: &[&str]) -> Vec<String> {
    ["writer-10001", "/work", "model", "effort", "/prompt"]
        .into_iter()
        .chain(extra.iter().copied())
        .map(str::to_owned)
        .collect()
}

#[test]
fn numbered_role_and_repeated_options() {
    let parsed = Arguments::parse(args(&[
        "--host",
        "first",
        "--dry-run",
        "--host",
        "second",
        "--ctx-guard",
        "1,2",
    ]))
    .unwrap();
    assert_eq!(parsed.role, "writer");
    assert_eq!(parsed.option("host"), Some("second"));
    assert_eq!(
        parsed.guard,
        Some(Caps {
            handoff: 1,
            hard: 2
        })
    );
    assert!(parsed.dry);
    assert!(matches!(
        Arguments::parse(args(&["--mcp"])),
        Err(ParseError::Usage)
    ));
    assert!(matches!(
        Arguments::parse(args(&["--ctx-guard", "2,1"])),
        Err(ParseError::Guard(_))
    ));
}

#[test]
fn cap_policy_and_saved_override() {
    let policy = serde_json::json!({
        "caps": {"default": {"handoff": 7, "hard": 8}, "roles": {"writer": {"handoff": 9, "hard": 10}}},
        "attended_window_s": 3600
    });
    let active = policy::select_caps(Some(&policy), "writer", None);
    assert_eq!(
        active.caps,
        Caps {
            handoff: 9,
            hard: 10
        }
    );
    assert_eq!(active.source, "role");
    assert_eq!(active.window, 3600);
    let invalid = serde_json::json!({"handoff": true, "hard": 2});
    let active = policy::select_caps(Some(&policy), "writer", Some(&invalid));
    assert_eq!(
        active.caps,
        Caps {
            handoff: 7,
            hard: 8
        }
    );
    assert_eq!(active.error, Some("invalid_override"));
}

#[test]
fn legacy_fallback_prefers_the_next_enabled_host_and_can_warn_and_keep_local() {
    use placement::Placement;
    use serde_json::json;
    let policy = json!({"hosts": [
        {"name": "previous", "memory_floor_gib": 2},
        {"name": "here", "memory_floor_gib": 2},
        {"name": "disabled", "memory_floor_gib": 1, "enabled": false},
        {"name": "later", "memory_floor_gib": 4}
    ]});
    let placement = Placement::with_hostname(Some(&policy), None, "here".into(), "here".into());
    let (host, notices) = placement
        .fallback(None, |host| {
            if host.is_some_and(|host| host.name() == "later") {
                Ok(4 << 30)
            } else {
                Ok(1 << 30)
            }
        })
        .unwrap();
    assert_eq!(host.name(), "later");
    assert_eq!(
        notices,
        ["fleet-launch: here below 2 GiB floor; selecting later"]
    );

    let (host, notices) = placement.fallback(None, |_| Err(())).unwrap();
    assert_eq!(host.name(), "here");
    assert_eq!(
        notices,
        [
            "fleet-launch: host later unreachable",
            "fleet-launch: ALERT no host above floor; launching here on here (no queue)"
        ]
    );
}

#[test]
fn future_attended_lease_is_deleted_and_window_is_bounded() {
    use std::{
        fs,
        time::{Duration, UNIX_EPOCH},
    };
    let dir = tempfile::tempdir().unwrap();
    let marker = dir.path().join("lease.json");
    let issued = UNIX_EPOCH + Duration::from_secs(2000);
    let write = |until| {
        fs::write(&marker, format!("{{\"until\":{until}}}")).unwrap();
        fs::File::options()
            .write(true)
            .open(&marker)
            .unwrap()
            .set_modified(issued)
            .unwrap();
    };
    write(2100);
    assert!(!policy::is_attended(&marker, 1800, 1000.0));
    assert!(!marker.exists());
    write(2100);
    assert!(policy::is_attended(&marker, 1800, 2050.0));
    assert!(!policy::is_attended(&marker, 99, 2050.0));
    assert!(!policy::is_attended(&marker, 1800, 2100.0));
}

#[test]
fn subprocess_capture_times_out_without_waiting_for_exit() {
    use std::{
        io::ErrorKind,
        process::Command,
        time::{Duration, Instant},
    };
    let start = Instant::now();
    let mut command = Command::new("/usr/bin/sleep");
    command.arg("10");
    let error = process::capture(&mut command, None, Duration::from_millis(20)).unwrap_err();
    assert_eq!(error.kind(), ErrorKind::TimedOut);
    assert!(start.elapsed() < Duration::from_secs(2));
}

fn role_args(extra: &[&str]) -> Vec<String> {
    let mut all = args(extra);
    all[0] = "writer".into();
    all
}

fn blocked_world() -> (tempfile::TempDir, std::path::PathBuf, Paths) {
    let dir = tempfile::tempdir().unwrap();
    let spawn = dir.path().join("spawn");
    let path = round_watch::state_file(&spawn, "writer-0884");
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(
        &path,
        json!({"actions": {}, "blocked": {"round_key": "k", "command": "cargo test -p x",
            "reason": "second wait over the cap in one round"}})
        .to_string(),
    )
    .unwrap();
    let routing = dir.path().join("routing");
    std::fs::create_dir_all(routing.join("launch")).unwrap();
    std::fs::write(
        routing.join("launch/writer.json"),
        json!({"session_name": "writer-0884", "session_id": "abc", "cwd": "/work"}).to_string(),
    )
    .unwrap();
    let paths = Paths {
        state: routing,
        home: dir.path().join("home"),
        ops: dir.path().join("ops"),
    };
    (dir, spawn, paths)
}

#[test]
fn a_role_only_respawn_of_a_blocked_session_is_refused_through_its_launch_record() {
    let (_dir, spawn, paths) = blocked_world();
    // The watchdog respawns by role: the block is under the numbered name in the record.
    let request =
        Request::new(Arguments::parse(role_args(&["--respawn"])).unwrap(), &paths).unwrap();
    let reason = round_blocked(&request, &spawn).expect("blocked");
    assert!(
        reason.contains("writer-0884") && reason.contains("cargo test -p x"),
        "{reason}"
    );
    assert!(reason.contains("second wait"), "{reason}");
    // By session name, too.
    let named = Arguments::parse(role_args(&["--respawn", "--session", "writer-0884"])).unwrap();
    let request = Request::new(named, &paths).unwrap();
    assert!(round_blocked(&request, &spawn).is_some());
}

#[test]
fn a_plain_launch_or_another_session_is_not_refused() {
    let (_dir, spawn, paths) = blocked_world();
    let plain = Request::new(Arguments::parse(role_args(&[])).unwrap(), &paths).unwrap();
    assert_eq!(round_blocked(&plain, &spawn), None, "a plain launch");
    assert_eq!(
        blocked_reason(true, &[Some("writer-0885"), None], &spawn),
        None
    );
    assert_eq!(blocked_reason(true, &[Some("../x")], &spawn), None);
}
