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

fn writer_policy(extra: serde_json::Value) -> serde_json::Value {
    let mut policy = serde_json::json!({"worker_roles": ["writer"], "hosts": [
        {"name": "ws", "memory_floor_gib": 2},
        {"name": "dell", "memory_floor_gib": 2},
        {"name": "vm", "memory_floor_gib": 2, "last_resort": true}
    ]});
    policy
        .as_object_mut()
        .unwrap()
        .extend(extra.as_object().unwrap().clone());
    policy
}

#[test]
fn a_writer_without_a_host_goes_to_the_worker_with_the_most_free_memory_and_the_last_resort_stays_last(
) {
    use placement::Placement;
    let policy = writer_policy(serde_json::json!({}));
    let placement =
        Placement::with_hostname(Some(&policy), Some("writer"), "vm".into(), "vm".into());
    let pick = |memory: [u64; 3]| {
        placement
            .select_with(None, |host, _| {
                Some(
                    memory[["ws", "dell", "vm"]
                        .iter()
                        .position(|n| *n == host.name())
                        .unwrap()]
                        << 10,
                )
            })
            .map(|host| host.name().to_owned())
    };
    assert_eq!(pick([105, 16, 90]).unwrap(), "ws");
    assert_eq!(pick([8, 30, 90]).unwrap(), "dell");
    assert_eq!(
        pick([1, 1, 90]).unwrap(),
        "vm",
        "coding.vm only when no worker admits"
    );
    assert!(pick([1, 1, 1]).is_err());
}

#[test]
fn a_writer_naming_a_host_is_refused_while_placement_is_on_unless_routing_dated_an_exception() {
    use placement::Placement;
    let refusal = |extra: serde_json::Value, role: &str, name: &str, today: &str| {
        let policy = writer_policy(extra);
        Placement::with_hostname(Some(&policy), Some(role), "vm".into(), "vm".into())
            .named_host_refusal(name, today)
    };
    let none = serde_json::json!({});
    assert!(refusal(none.clone(), "writer", "dell", "2026-11-24")
        .unwrap()
        .contains("may not name a host"));
    assert!(
        refusal(none, "reviewer", "dell", "2026-11-24").is_none(),
        "not a worker role"
    );
    let exception = |until: &str, host: &str| serde_json::json!({"host_exceptions": [{"host": host, "decision": "D-6741", "until": until}]});
    assert!(refusal(
        exception("2026-11-30", "dell"),
        "writer",
        "dell",
        "2026-11-24"
    )
    .is_none());
    assert!(refusal(
        exception("2026-11-30", "dell"),
        "writer",
        "dell",
        "2026-11-30"
    )
    .is_none());
    assert!(
        refusal(
            exception("2026-11-23", "dell"),
            "writer",
            "dell",
            "2026-11-24"
        )
        .is_some(),
        "expired"
    );
    assert!(
        refusal(
            exception("2026-11-30", "ws"),
            "writer",
            "dell",
            "2026-11-24"
        )
        .is_some(),
        "other host"
    );
    let unlisted = Placement::with_hostname(None, Some("writer"), "vm".into(), "vm".into());
    assert!(
        unlisted.named_host_refusal("vm", "2026-11-24").is_none(),
        "no policy: placement is off"
    );
}

#[test]
fn today_is_an_iso_date() {
    let today = placement::today();
    assert_eq!(today.len(), 10);
    assert!(today.starts_with("20") && today.as_bytes()[4] == b'-' && today.as_bytes()[7] == b'-');
}
