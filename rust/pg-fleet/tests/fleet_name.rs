use pg_fleet::fleet_name::{choose, live_name, probe_remote, role_of};
use serde_json::json;

#[test]
fn counters_live_status_and_newest_order() {
    assert_eq!(role_of("build-L1-0412"), "build-L1");
    assert_eq!(role_of("build-L1-12"), "build-L1-12");
    let rows = json!([null,1,"bad",{"name":"routing-0001","status":"idle","startedAt":1},{"name":"routing-0002","status":"busy","startedAt":2},{"name":"routing-9999","startedAt":999},{"name":"other","status":"idle"}]);
    assert_eq!(
        live_name("routing", rows.as_array().unwrap()),
        Some("routing-0002".into())
    );
}
#[test]
fn confirmed_overlap_and_malformed_listing_fallback() {
    let spec = json!({"session_id":"replacement","session_name":"routing-0001"});
    let remote = json!([{"sessionId":"old","name":"routing-0999","status":"idle","startedAt":100},{"sessionId":"replacement","name":"routing-0042","status":"idle"}]);
    for local in [
        json!([]),
        json!("stale"),
        json!(null),
        json!(42),
        json!({}),
        json!([null, 1, "bad"]),
    ] {
        assert_eq!(
            choose("routing", &local, remote.as_array().unwrap(), &spec),
            Some("routing-0042".into())
        );
        assert_eq!(
            choose("routing", &local, &[], &spec),
            Some("routing-0001".into())
        );
    }
    assert_eq!(choose("routing", &json!([]), &[], &json!({})), None);
}
#[test]
fn remote_transport_retry_bad_json_and_unknown_not_dead() {
    for (outcomes, want_calls, want_errors) in [
        (vec![Ok("[{\"status\":\"busy\"}]".into())], 1, 0),
        (vec![Err("TimeoutExpired".into()), Ok("[]".into())], 2, 0),
        (vec![Err("OSError".into()), Ok("[]".into())], 2, 0),
        (
            vec![Err("OSError".into()), Err("TimeoutExpired".into())],
            2,
            1,
        ),
        (vec![Ok("not json".into())], 1, 1),
        (vec![Ok("{}".into())], 1, 1),
    ] {
        let mut calls = 0;
        let mut waits = 0;
        let mut outcomes = outcomes.into_iter();
        let (rows, errors) = probe_remote(
            "peer",
            || {
                calls += 1;
                outcomes.next().unwrap()
            },
            || waits += 1,
        );
        assert_eq!(calls, want_calls);
        assert_eq!(waits, want_calls - 1);
        assert_eq!(errors.len(), want_errors);
        if !rows.is_empty() {
            assert_eq!(rows[0]["host"], "peer");
        }
        if want_errors > 0 {
            assert_eq!(
                errors[0],
                "host peer unreachable (sessions unknown, not dead)"
            );
        }
    }
}

#[test]
fn cli_fallback_unknown_usage_and_live_confirmed() {
    let d = tempfile::tempdir().unwrap();
    let launch = d.path().join("launch");
    std::fs::create_dir(&launch).unwrap();
    let policy = d.path().join("policy.json");
    std::fs::write(&policy, "{}").unwrap();
    let run = |args: &[&str]| {
        std::process::Command::new(env!("CARGO_BIN_EXE_fleet-name"))
            .args(args)
            .env("HOME", d.path())
            .env("PATH", d.path())
            .env("FLEET_HOST_POLICY", &policy)
            .env("ROUTING_STATE_DIR", d.path())
            .output()
            .unwrap()
    };
    assert_eq!(run(&[]).status.code(), Some(2));
    let out = run(&["routing"]);
    assert_eq!(out.status.code(), Some(1));
    assert_eq!(
        String::from_utf8_lossy(&out.stderr),
        "fleet-name: no live session or launch record for routing\n"
    );
    std::fs::write(
        launch.join("routing.json"),
        r#"{"session_id":"replacement","session_name":"routing-0001"}"#,
    )
    .unwrap();
    assert_eq!(
        String::from_utf8_lossy(&run(&["routing-0042"]).stdout),
        "routing-0001\n"
    );
    let payload = r#"[{"sessionId":"old","name":"routing-0999","status":"idle","startedAt":100},{"sessionId":"replacement","name":"routing-0001","status":"idle"}]"#;
    #[cfg(windows)]
    std::fs::write(
        d.path().join("claude.cmd"),
        format!("@echo off\r\necho {payload}\r\n"),
    )
    .unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let p = d.path().join("claude");
        std::fs::write(&p, format!("#!/bin/sh\nprintf '%s\\n' '{payload}'\n")).unwrap();
        std::fs::set_permissions(p, std::fs::Permissions::from_mode(0o700)).unwrap();
    }
    assert_eq!(
        String::from_utf8_lossy(&run(&["routing"]).stdout),
        "routing-0001\n"
    );
}
