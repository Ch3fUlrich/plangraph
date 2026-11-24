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

#[test]
fn a_batch_brief_for_a_held_item_is_refused_unless_overridden_with_a_reason() {
    let dir = tempfile::tempdir().unwrap();
    let run = dir.path().join("state/runs/run-a");
    std::fs::create_dir_all(&run).unwrap();
    let record = serde_json::json!({"run_id":"run-a","run_kind":"native-codex","status":"running"});
    std::fs::write(run.join("record.json"), record.to_string()).unwrap();
    let request = serde_json::json!({"prompt":"Item key: demo:FIXTURE\nNAME: held-lane\n"});
    std::fs::write(run.join("native-request.json"), request.to_string()).unwrap();
    let dirs = (dir.path().join("state"), dir.path().join("sessions"));
    let dirs = (dirs.0.as_path(), dirs.1.as_path());

    let fixture = include_str!("../../tests/fixtures/batch-writer-input.txt");
    let brief = crate::batch_writer_brief::render(fixture)
        .unwrap()
        .into_owned();
    let refused = guard_lanes_in(dirs, fixture, &brief)
        .unwrap_err()
        .to_string();
    assert!(
        refused.contains("demo:FIXTURE is held by native run held-lane (run-a)"),
        "{refused}"
    );

    let with =
        |reason: &str| fixture.replacen('{', &format!("{{\"lane_override\":\"{reason}\","), 1);
    let blank = guard_lanes_in(dirs, &with(""), &brief)
        .unwrap_err()
        .to_string();
    assert!(blank.contains("non-empty reason"), "{blank}");
    guard_lanes_in(dirs, &with("retry after a wedged lane"), &brief).unwrap();
    let log = std::fs::read_to_string(dirs.0.join("lane-overrides.jsonl")).unwrap();
    assert!(
        log.contains("retry after a wedged lane") && log.contains("\"context\":\"brief\""),
        "{log}"
    );
}
