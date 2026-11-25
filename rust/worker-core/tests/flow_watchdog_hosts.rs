use std::{fs, process::Command};

/// The key hygiene writes for the root filesystem: its device number.
#[cfg(unix)]
fn root_key() -> String {
    use std::os::unix::fs::MetadataExt;
    fs::metadata("/").unwrap().dev().to_string()
}

#[cfg(not(unix))]
fn root_key() -> String {
    "0".into()
}

fn active(host: serde_json::Value) -> String {
    run(host).0
}

/// The `active` file and the queued alert messages of one watchdog pass.
fn run(host: serde_json::Value) -> (String, Vec<String>) {
    run_with(host, &[])
}

fn run_with(host: serde_json::Value, env: &[(&str, &std::path::Path)]) -> (String, Vec<String>) {
    let dir = tempfile::tempdir().unwrap();
    pass_in(dir.path(), host, env)
}

/// One watchdog pass over `dir`, which keeps the state between passes.
fn pass_in(
    dir: &std::path::Path,
    host: serde_json::Value,
    env: &[(&str, &std::path::Path)],
) -> (String, Vec<String>) {
    pass_hosts_in(dir, vec![host], env)
}

/// As `pass_in` with any host list, including none (a missing sample).
fn pass_hosts_in(
    dir: &std::path::Path,
    hosts: Vec<serde_json::Value>,
    env: &[(&str, &std::path::Path)],
) -> (String, Vec<String>) {
    let snapshot = serde_json::json!({"pace":66,"budget":66,"ci_failed":0,
        "ci_total":10,"infra_failed":0,"review_age_hours":0,"review_rounds":0,
        "critical_idle_hours":0,"coding_vm_runs":0,"worker_idle":false,
        "host_drift":false,"closes_recent":4,"closes_previous":4,"hosts":hosts});
    let input = dir.join("input.json");
    fs::write(&input, snapshot.to_string()).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_flow-watchdog"))
        .args([input.as_os_str(), dir.as_os_str()])
        .env_remove("PLANGRAPH_WATCHDOG_DELIVERY")
        .env_remove("PLANGRAPH_FLOW_PUSH")
        .env_remove("PLANGRAPH_DISK_WATERMARKS")
        .env("PLANGRAPH_HOST_NAME", "fixture")
        .envs(env.iter().copied())
        .output()
        .unwrap();
    assert!(output.status.success(), "{:?}", output);
    let queue: Vec<serde_json::Value> = fs::read(dir.join("queue.json"))
        .ok()
        .map(|bytes| serde_json::from_slice(&bytes).unwrap())
        .unwrap_or_default();
    let messages = queue
        .iter()
        .map(|event| event["message"].as_str().unwrap().to_owned())
        .collect();
    (
        fs::read_to_string(dir.join("active")).unwrap_or_default(),
        messages,
    )
}

#[test]
fn disk_usage_is_strictly_above_85_on_either_filesystem() {
    for (tmp, root, fires) in [(85.0, 85.0, false), (85.1, 0.0, true), (0.0, 85.1, true)] {
        let result = active(serde_json::json!({"host":"fixture","tmp_used_percent":tmp,
            "root_used_percent":root,"checkout_head":"a","origin_main_head":"a",
            "behind_commits":0,"age_hours":100}));
        assert_eq!(result.contains("host-disk"), fires, "{result}");
    }
}

#[test]
fn hot_work_disks_raise_one_host_alarm() {
    let result = active(serde_json::json!({"host":"fixture","tmp_used_percent":86,
        "root_used_percent":0,"work_used_percent":[90,99],"checkout_head":"a",
        "origin_main_head":"a","behind_commits":0,"age_hours":100}));
    assert_eq!(result.matches("host-disk").count(), 1, "{result}");
    let result = active(serde_json::json!({"host":"fixture","tmp_used_percent":0,
        "root_used_percent":0,"work_used_percent":[85,85.1],"checkout_head":"a",
        "origin_main_head":"a","behind_commits":0,"age_hours":100}));
    assert_eq!(result.matches("host-disk").count(), 1, "{result}");
}

#[test]
fn checkout_alerts_by_commit_count_or_age_only_when_behind() {
    for (head, commits, age, fires) in [
        ("a", 10, 24.0, false),
        ("a", 11, 0.0, true),
        ("a", 1, 24.1, true),
        ("a", 1, 48.0, true),
        ("b", 0, 48.0, false),
        ("a", 0, 48.0, false),
    ] {
        let result = active(serde_json::json!({"host":"fixture","tmp_used_percent":0,
            "root_used_percent":0,"checkout_head":head,"origin_main_head":"b",
            "behind_commits":commits,"age_hours":age}));
        assert_eq!(result.contains("host-code"), fires, "{result}");
        if fires {
            assert!(result.contains("host-code: host=\"fixture\""));
        }
    }
}

#[test]
fn cat_hook_test_crates_are_unix_only() {
    for source in [
        include_str!("flow_watchdog.rs"),
        include_str!("flow_watchdog_regressions.rs"),
    ] {
        assert!(source.starts_with("#![cfg(unix)]"));
    }
}

#[test]
fn host_disk_alert_at_86_percent_names_the_five_largest_consumers_once() {
    let consumers: Vec<_> = (1..=7u64)
        .map(|n| serde_json::json!({"path": format!("/var/c{n}"), "bytes": n * 1000}))
        .collect();
    let host = serde_json::json!({"host":"fixture","tmp_used_percent":0,
        "root_used_percent":86,"top_consumers":consumers,"checkout_head":"a",
        "origin_main_head":"a","behind_commits":0,"age_hours":100});
    let (active, messages) = run(host.clone());
    assert_eq!(active.matches("host-disk").count(), 1, "{active}");
    assert!(
        !active.contains("c7"),
        "the dedupe key must not move with sizes: {active}"
    );
    assert_eq!(messages.len(), 1, "{messages:?}");
    let message = &messages[0];
    for kept in [
        "/var/c7=7000",
        "/var/c6=6000",
        "/var/c5=5000",
        "/var/c4=4000",
        "/var/c3=3000",
    ] {
        assert!(message.contains(kept), "{message}");
    }
    assert!(
        !message.contains("/var/c2=") && !message.contains("/var/c1="),
        "{message}"
    );
    assert!(
        message.find("/var/c7").unwrap() < message.find("/var/c6").unwrap(),
        "{message}"
    );
}

#[test]
fn the_local_hosts_alert_names_the_consumers_the_hygiene_watermark_measured() {
    let state = tempfile::tempdir().unwrap();
    let marks = state.path().join("disk-watermarks.json");
    // The shape pg-disk-hygiene writes: device -> {free, total, directories}.
    let rows: Vec<_> = (1..=7u64)
        .map(|n| serde_json::json!({"path": format!("/data/d{n}"), "bytes": n * 10}))
        .collect();
    fs::write(
        &marks,
        serde_json::json!({root_key(): {"free": 1, "total": 10, "directories": rows}}).to_string(),
    )
    .unwrap();
    let host = serde_json::json!({"host":"fixture","tmp_used_percent":0,"root_used_percent":87,
        "checkout_head":"a","origin_main_head":"a","behind_commits":0,"age_hours":100});
    let (_, messages) = run_with(host.clone(), &[("PLANGRAPH_DISK_WATERMARKS", &marks)]);
    assert_eq!(messages.len(), 1, "{messages:?}");
    assert!(
        messages[0].contains("/data/d7=70") && messages[0].contains("/data/d3=30"),
        "{messages:?}"
    );
    assert!(!messages[0].contains("/data/d2="), "{messages:?}");
    // Another host's entry never borrows this host's measurements.
    let other = serde_json::json!({"host":"elsewhere","tmp_used_percent":0,"root_used_percent":87,
        "checkout_head":"a","origin_main_head":"a","behind_commits":0,"age_hours":100});
    let (_, messages) = run_with(other, &[("PLANGRAPH_DISK_WATERMARKS", &marks)]);
    assert!(!messages[0].contains("/data/"), "{messages:?}");
}

#[test]
fn an_alert_queued_before_hygiene_sampled_is_followed_by_one_naming_the_consumers() {
    let state = tempfile::tempdir().unwrap();
    let watch = tempfile::tempdir().unwrap();
    let marks = state.path().join("disk-watermarks.json");
    let host = serde_json::json!({"host":"fixture","tmp_used_percent":0,"root_used_percent":86,
        "checkout_head":"a","origin_main_head":"a","behind_commits":0,"age_hours":100});
    let env = [("PLANGRAPH_DISK_WATERMARKS", marks.as_path())];
    // Pass 1: 86% seen, the thirty-minute hygiene timer has not written its watermark yet.
    let (_, queued) = pass_in(watch.path(), host.clone(), &env);
    assert_eq!(queued.len(), 1, "{queued:?}");
    assert!(!queued[0].contains("top="), "{queued:?}");
    // Pass 2 (same state): hygiene has written it.
    let rows = serde_json::json!([{"path": "/data/big", "bytes": 900}, {"path": "/data/small", "bytes": 1}]);
    fs::write(
        &marks,
        serde_json::json!({root_key(): {"free": 1, "total": 10, "directories": rows}}).to_string(),
    )
    .unwrap();
    let (active, queued) = pass_in(watch.path(), host.clone(), &env);
    assert_eq!(
        queued.len(),
        2,
        "the measured alert is queued once more: {queued:?}"
    );
    assert!(queued[1].contains("/data/big=900"), "{queued:?}");
    // Pass 3: sizes move, nothing new is queued.
    let rows = serde_json::json!([{"path": "/data/big", "bytes": 950}]);
    fs::write(
        &marks,
        serde_json::json!({root_key(): {"free": 1, "total": 10, "directories": rows}}).to_string(),
    )
    .unwrap();
    let (again, queued) = pass_in(watch.path(), host, &env);
    assert_eq!((active, queued.len()), (again, 2));
}

#[test]
fn a_crossing_queues_at_most_one_bare_and_one_measured_alert_however_the_measurement_flaps() {
    let state = tempfile::tempdir().unwrap();
    let watch = tempfile::tempdir().unwrap();
    let marks = state.path().join("disk-watermarks.json");
    let host = serde_json::json!({"host":"fixture","tmp_used_percent":0,"root_used_percent":87,
        "checkout_head":"a","origin_main_head":"a","behind_commits":0,"age_hours":100});
    let env = [("PLANGRAPH_DISK_WATERMARKS", marks.as_path())];
    let write = |present: bool| {
        if present {
            let rows = serde_json::json!([{"path": "/data/big", "bytes": 900}]);
            fs::write(
                &marks,
                serde_json::json!({root_key(): {"free": 1, "total": 10, "directories": rows}})
                    .to_string(),
            )
            .unwrap();
        } else {
            let _ = fs::remove_file(&marks);
        }
    };
    // Consumers present, absent, present, absent, present: the first sighting queues the
    // measured alert, nothing after it queues anything.
    for (pass, present) in [true, false, true, false, true].into_iter().enumerate() {
        write(present);
        let (active, queued) = pass_in(watch.path(), host.clone(), &env);
        assert_eq!(queued.len(), 1, "pass {pass}: {queued:?}");
        assert_eq!(active.lines().count(), 1, "pass {pass}: {active}");
    }
    // The same flapping after a bare first alert: one bare, one measured, no more.
    let again = tempfile::tempdir().unwrap();
    for (pass, present) in [false, true, false, true].into_iter().enumerate() {
        write(present);
        let (_, queued) = pass_in(again.path(), host.clone(), &env);
        assert_eq!(
            queued.len(),
            if pass == 0 { 1 } else { 2 },
            "pass {pass}: {queued:?}"
        );
    }
    // The crossing ends: both keys leave `active`, and the next crossing alerts afresh.
    let calm = serde_json::json!({"host":"fixture","tmp_used_percent":0,"root_used_percent":10,
        "checkout_head":"a","origin_main_head":"a","behind_commits":0,"age_hours":100});
    let (active, _) = pass_in(again.path(), calm, &env);
    assert!(active.trim().is_empty(), "{active}");
}

#[test]
fn a_missing_host_sample_keeps_the_crossing_active_and_only_a_sample_ends_it() {
    let state = tempfile::tempdir().unwrap();
    let watch = tempfile::tempdir().unwrap();
    let marks = state.path().join("disk-watermarks.json");
    let env = [("PLANGRAPH_DISK_WATERMARKS", marks.as_path())];
    let rows = serde_json::json!([{"path": "/data/big", "bytes": 900}]);
    fs::write(
        &marks,
        serde_json::json!({root_key(): {"free": 1, "total": 10, "directories": rows}}).to_string(),
    )
    .unwrap();
    let hot = serde_json::json!({"host":"fixture","tmp_used_percent":0,"root_used_percent":87,
        "checkout_head":"a","origin_main_head":"a","behind_commits":0,"age_hours":100});
    let calm = serde_json::json!({"host":"fixture","tmp_used_percent":0,"root_used_percent":10,
        "checkout_head":"a","origin_main_head":"a","behind_commits":0,"age_hours":100});
    let (_, queued) = pass_hosts_in(watch.path(), vec![hot.clone()], &env);
    assert_eq!(queued.len(), 1, "{queued:?}");
    // The collector missed this host once: the crossing is still the same crossing.
    let (active, queued) = pass_hosts_in(watch.path(), vec![], &env);
    assert!(
        active.contains("host-disk"),
        "kept through the missing sample: {active}"
    );
    assert_eq!(queued.len(), 1, "{queued:?}");
    let (_, queued) = pass_hosts_in(watch.path(), vec![hot.clone()], &env);
    assert_eq!(
        queued.len(),
        1,
        "no duplicate for the same crossing: {queued:?}"
    );
    // A sample that shows recovery ends it; the next crossing alerts again.
    let (active, _) = pass_hosts_in(watch.path(), vec![calm], &env);
    assert!(!active.contains("host-disk"), "{active}");
    let (_, queued) = pass_hosts_in(watch.path(), vec![hot], &env);
    assert_eq!(queued.len(), 2, "{queued:?}");
}

#[cfg(unix)]
#[test]
fn a_root_disk_alert_names_only_the_directories_measured_on_the_root_filesystem() {
    use std::os::unix::fs::MetadataExt;
    let root_dev = fs::metadata("/").unwrap().dev();
    let work_dev = root_dev + 1; // a separate work mount
    let state = tempfile::tempdir().unwrap();
    let marks = state.path().join("disk-watermarks.json");
    let env = [("PLANGRAPH_DISK_WATERMARKS", marks.as_path())];
    let both = serde_json::json!({
        root_dev.to_string(): {"free": 1, "total": 10, "directories": [{"path": "/rootfs/big", "bytes": 500}]},
        work_dev.to_string(): {"free": 1, "total": 10, "directories": [{"path": "/work/huge", "bytes": 9000}]},
    });
    fs::write(&marks, both.to_string()).unwrap();
    let host = |root: u32, work: Vec<u32>| {
        serde_json::json!({"host":"fixture","tmp_used_percent":0,"root_used_percent":root,
        "work_used_percent":work,"checkout_head":"a","origin_main_head":"a","behind_commits":0,"age_hours":100})
    };
    // Only the root crossed: its row, not the larger work mount's.
    let (_, queued) = run_with(host(87, vec![]), &env);
    assert!(queued[0].contains("/rootfs/big=500"), "{queued:?}");
    assert!(!queued[0].contains("/work/huge"), "{queued:?}");
    // Only a work mount crossed: its row, not the root's.
    let (_, queued) = run_with(host(10, vec![90]), &env);
    assert!(
        queued[0].contains("/work/huge=9000") && !queued[0].contains("/rootfs/big"),
        "{queued:?}"
    );
    // The root crossed but only the work mount has been measured: a bare alert, not "measured".
    fs::write(
        &marks,
        serde_json::json!({work_dev.to_string(): both[work_dev.to_string()]}).to_string(),
    )
    .unwrap();
    let (active, queued) = run_with(host(87, vec![]), &env);
    assert!(
        !queued[0].contains("top=") && !active.contains("measured"),
        "{queued:?} {active}"
    );
}
