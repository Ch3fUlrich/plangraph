use super::*;

fn fixture() -> PathBuf {
    let path = std::env::temp_dir().join(format!(
        "pg-agent-view-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    fs::create_dir_all(&path).unwrap();
    path
}
fn put(home: &Path, path: &str, value: Value) {
    let path = home.join(path);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, value.to_string()).unwrap();
}
#[test]
fn sources_sidecars_finished_and_bad_rows() {
    let home = fixture();
    let state = home.join(".local/state/plangraph");
    let record = json!({"run_id":"worker","status":"running","started":10,"item_key":"PROJECT:ITEM","credential":"excluded"});
    for path in [
        ".local/state/plangraph/runs/one/record.json",
        ".local/state/plangraph/sessions/one.json",
        ".claude/jobs/one/state.json",
        "lane.json",
    ] {
        put(&home, path, record.clone());
    }
    put(
        &home,
        ".config/autoos/oc-l1.json",
        json!({"lanes":{"one":{"state_file":home.join("lane.json")}}}),
    );
    put(
        &home,
        ".local/state/plangraph/sessions/_counters.json",
        json!({}),
    );
    put(
        &home,
        ".local/state/plangraph/sessions/one.ready.json",
        json!({}),
    );
    put(
        &home,
        ".claude/jobs/done/state.json",
        json!({"state":"done"}),
    );
    let data = local(&home, &state, "local", 20.0);
    assert_eq!(data["agents"].as_array().unwrap().len(), 1);
    assert!(data["agents"]
        .as_array()
        .unwrap()
        .iter()
        .all(|r| r["age_s"] == 10.0 && r["name"] == "worker"));
    assert!(!data.to_string().contains("excluded"));
    fs::write(home.join(".claude/jobs/one/state.json"), "{").unwrap();
    let partial = local(&home, &state, "local", 20.0);
    assert_eq!(partial["readable"], false);
    assert_eq!(partial["reason"], "source_unreadable");
    assert_eq!(partial["agents"][0]["runtime"], "spawner-run");
    assert_eq!(
        local(&home, &state, "local", 20.0)["agents"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    fs::remove_dir_all(home).unwrap();
}
#[test]
fn unreadable_is_not_zero() {
    let home = fixture();
    put(&home, ".config/autoos/oc-l1.json", json!({"lanes":false}));
    assert_eq!(
        local(&home, &home.join("absent"), "local", 0.0)["readable"],
        false
    );
    fs::remove_dir_all(home).unwrap();
}
#[test]
fn plain_entries_are_empty_but_absent_lane_is_unknown() {
    let home = fixture();
    empty_sources(&home);
    put(&home, "state/runs/plain", json!({}));
    put(&home, ".claude/jobs/plain", json!({}));
    let data = local(&home, &home.join("state"), "local", 0.0);
    assert_eq!(data["readable"], true);
    assert_eq!(data["agents"], json!([]));
    put(
        &home,
        ".config/autoos/oc-l1.json",
        json!({"lanes":[{"state_file":home.join("absent.json")}]}),
    );
    let data = local(&home, &home.join("state"), "local", 0.0);
    assert_eq!(data["readable"], false);
    assert_eq!(data["reason"], "source_unreadable");
    assert_eq!(data["agents"], json!([]));
    put(&home, "absent.json", json!([]));
    let data = local(&home, &home.join("state"), "local", 0.0);
    assert_eq!(data["readable"], false);
    assert_eq!(data["reason"], "source_unreadable");
    fs::remove_dir_all(home).unwrap();
}
fn empty_sources(home: &Path) {
    for path in ["state/runs", "state/sessions", ".claude/jobs"] {
        fs::create_dir_all(home.join(path)).unwrap();
    }
}
#[test]
fn present_empty_sources_are_zero() {
    let home = fixture();
    empty_sources(&home);
    let data = local(&home, &home.join("state"), "local", 0.0);
    assert_eq!(data["readable"], true);
    assert_eq!(data["reason"], Value::Null);
    assert_eq!(data["agents"], json!([]));
    fs::remove_dir_all(home).unwrap();
}
#[test]
fn each_missing_source_directory_is_unknown() {
    for path in ["state/runs", "state/sessions", ".claude/jobs", "state"] {
        let home = fixture();
        empty_sources(&home);
        fs::remove_dir_all(home.join(path)).unwrap();
        let data = local(&home, &home.join("state"), "local", 0.0);
        assert_eq!(data["readable"], false, "{path}");
        assert_eq!(data["reason"], "source_unreadable", "{path}");
        assert_eq!(data["agents"], json!([]));
        fs::remove_dir_all(home).unwrap();
    }
}
#[cfg(unix)]
#[test]
fn permission_denied_sources_are_unknown() {
    use std::os::unix::fs::PermissionsExt;
    if Command::new("id").arg("-u").output().unwrap().stdout == b"0\n" {
        eprintln!("SKIP permission_denied_sources_are_unknown: root bypasses mode 000 permissions");
        return;
    }
    for path in [
        "state",
        "state/runs",
        "state/sessions",
        ".claude/jobs",
        "lane.json",
        ".config/autoos/oc-l1.json",
        "state/runs/one/record.json",
        "state/sessions/one.json",
        ".claude/jobs/one/state.json",
    ] {
        let home = fixture();
        empty_sources(&home);
        for record in [
            "state/runs/one/record.json",
            "state/sessions/one.json",
            ".claude/jobs/one/state.json",
        ] {
            put(&home, record, json!({"name":"worker"}));
        }
        put(&home, "lane.json", json!({"name":"worker"}));
        put(
            &home,
            ".config/autoos/oc-l1.json",
            json!({"lanes":[{"state_file":home.join("lane.json")}]}),
        );
        let target = home.join(path);
        let permissions = fs::metadata(&target).unwrap().permissions();
        fs::set_permissions(&target, fs::Permissions::from_mode(0o000)).unwrap();
        let data = local(&home, &home.join("state"), "local", 0.0);
        fs::set_permissions(&target, permissions).unwrap();
        assert_eq!(data["readable"], false, "{path}");
        assert_eq!(data["reason"], "source_unreadable", "{path}");
        fs::remove_dir_all(home).unwrap();
    }
}
#[test]
fn missing_record_files_are_unknown() {
    for path in ["state/runs/one", ".claude/jobs/one"] {
        let home = fixture();
        empty_sources(&home);
        fs::create_dir_all(home.join(path)).unwrap();
        let data = local(&home, &home.join("state"), "local", 0.0);
        assert_eq!(data["readable"], false, "{path}");
        assert_eq!(data["reason"], "source_unreadable", "{path}");
        assert_eq!(data["agents"], json!([]));
        fs::remove_dir_all(home).unwrap();
    }
}
#[test]
fn config_read_error_is_unknown() {
    let home = fixture();
    empty_sources(&home);
    fs::create_dir_all(home.join(".config/autoos/oc-l1.json")).unwrap();
    let data = local(&home, &home.join("state"), "local", 0.0);
    assert_eq!(data["readable"], false);
    assert_eq!(data["reason"], "source_unreadable");
    assert_eq!(data["agents"], json!([]));
    fs::remove_dir_all(home).unwrap();
}
#[test]
fn collector_units_are_local_and_ordered() {
    let service = include_str!("../../../deploy/systemd/plangraph-agents-view.service");
    for line in [
        "Type=oneshot",
        "Before=plangraph-render.service",
        "ExecStart=%h/.local/bin/pg-spawnd agents-view --write --local",
        "Nice=10",
        "NoNewPrivileges=true",
        "IPAddressDeny=any",
    ] {
        assert!(service.lines().any(|actual| actual == line));
    }
    let timer = include_str!("../../../deploy/systemd/plangraph-agents-view.timer");
    for line in [
        "OnBootSec=1min",
        "OnUnitActiveSec=5min",
        "AccuracySec=15s",
        "WantedBy=timers.target",
    ] {
        assert!(timer.lines().any(|actual| actual == line));
    }
}
#[test]
fn missing_run_ids_keep_distinct_source_files() {
    let home = fixture();
    for (id, name) in [
        ("one", Value::Null),
        ("two", Value::Null),
        ("three", json!("shared")),
        ("four", json!("shared")),
    ] {
        put(
            &home,
            &format!("state/sessions/{id}.json"),
            json!({"name":name}),
        );
    }
    let data = local(&home, &home.join("state"), "local", 20.0);
    assert_eq!(data["agents"].as_array().unwrap().len(), 4);
    fs::remove_dir_all(home).unwrap();
}
#[test]
fn source_failure_does_not_skip_other_sources() {
    let home = fixture();
    let state = home.join("state");
    fs::write(&state, "not a directory").unwrap();
    put(
        &home,
        ".claude/jobs/live/state.json",
        json!({"name":"worker"}),
    );
    let data = local(&home, &state, "local", 20.0);
    assert_eq!(data["readable"], false);
    assert_eq!(data["agents"].as_array().unwrap().len(), 1);
    fs::remove_dir_all(home).unwrap();
}
#[test]
fn identities_are_run_ids_not_names() {
    let home = fixture();
    for id in ["one", "two"] {
        put(
            &home,
            &format!("state/runs/{id}/record.json"),
            json!({"run_id":id,"name":"worker","model":"preferred"}),
        );
        put(
            &home,
            &format!(".claude/jobs/{id}/state.json"),
            json!({"run_id":id,"name":"worker","model":"other"}),
        );
    }
    let data = local(&home, &home.join("state"), "local", 20.0);
    let rows = data["agents"].as_array().unwrap();
    assert_eq!(rows.len(), 2);
    assert!(rows.iter().all(|r| r["model"] == "preferred"));
    fs::remove_dir_all(home).unwrap();
}
#[test]
fn remote_validation_and_quotes() {
    assert_eq!(
        remote("remote", &json!({}), Duration::ZERO)["reason"],
        "no_route"
    );
    for bytes in [
        b"invalid".as_slice(),
        br#"{"hosts":["bad"]}"#,
        br#"{"hosts":[{"readable":true,"agents":["bad"]}]}"#,
    ] {
        assert_eq!(response("remote", bytes)["reason"], "invalid_response");
    }
    assert_eq!(
        response("remote", br#"{"hosts":[{"readable":false}]}"#)["readable"],
        false
    );
    assert_eq!(quote("/source with space"), "'/source with space'");
    assert_eq!(quote("a'b"), "'a'\\''b'");
}
#[test]
fn parallel_outer_deadline_and_slack() {
    let entries = json!({"slow":{},"fast":{}}).as_object().unwrap().clone();
    let start = Instant::now();
    let rows = gather(&entries, Duration::from_millis(20), |host, _, _| {
        thread::sleep(Duration::from_millis(if host == "slow" { 700 } else { 40 }));
        json!({"host":host,"readable":true})
    });
    assert!(start.elapsed() < Duration::from_millis(500));
    assert!(rows
        .iter()
        .any(|r| r["host"] == "fast" && r["readable"] == true));
    assert!(rows
        .iter()
        .any(|r| r["host"] == "slow" && r["reason"] == "timeout"));
}
#[path = "agent_view_guard_tests.rs"]
mod guard;
#[test]
fn private_atomic_projection() {
    let home = fixture();
    let path = home.join("agents-view.json");
    let data = project_view(
        &json!({"hosts":[{"host":"local","readable":true,"agents":[{"credential":"excluded"}]}]}),
    );
    pg_worker_core::run::record_io::atomic_write_private(&path, data.to_string().as_bytes())
        .unwrap();
    assert!(!fs::read_to_string(&path).unwrap().contains("excluded"));
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }
    fs::remove_dir_all(home).unwrap();
}
