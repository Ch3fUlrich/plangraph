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

struct Held;
impl crate::lane_guard::LaneSource for Held {
    fn lanes(&self, _: &[String]) -> Vec<crate::lane_guard::Lane> {
        vec![crate::lane_guard::Lane {
            name: "writer-0673".into(),
            keys: vec!["demo:HELD".into()],
            source: "run",
        }]
    }
}

fn prompt_file(text: &str) -> tempfile::NamedTempFile {
    let file = tempfile::NamedTempFile::new().unwrap();
    std::fs::write(file.path(), text).unwrap();
    file
}

#[test]
fn writer_launch_refuses_a_key_held_by_another_live_lane() {
    let file = prompt_file("Writer brief\n### demo:HELD | held item\nWhy: x\n");
    let error = read_admitted_prompt(file.path(), Some(&Held))
        .err()
        .expect("a held key is refused");
    assert!(error
        .to_string()
        .starts_with("lane_collision: demo:HELD is held by live lane writer-0673"));
    // Without the admission (restart, remote forward) the prompt is read as before.
    assert!(read_admitted_prompt(file.path(), None).is_ok());
}

#[test]
fn writer_launch_records_the_override_reason_and_the_items_of_the_brief() {
    let file = prompt_file(
        "Writer brief\n### demo:HELD | held item\nWhy: x\nLANE_OVERRIDE: fix lane on purpose\n",
    );
    let admitted = read_admitted_prompt(file.path(), Some(&Held)).unwrap();
    assert_eq!(
        admitted.lane_override.as_deref(),
        Some("fix lane on purpose")
    );
    assert_eq!(admitted.items, ["demo:HELD"]);
}
