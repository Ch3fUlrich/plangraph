#![cfg(target_os = "linux")]
use pg_worker_core::{
    disk_hygiene::{Guards, Report},
    disk_runtime::{disk_usage, shared_cache},
    host_command,
};
use std::{
    fs,
    process::Command,
    time::{Duration, Instant},
};
#[test]
fn disk_hygiene_usage_needs_no_df_and_bad_path_errors() {
    let tmp = tempfile::tempdir().unwrap();
    let free = disk_usage(tmp.path()).unwrap();
    assert!(free > 0);
    assert!(disk_usage(&tmp.path().join("absent")).is_err());
}
#[test]
fn disk_hygiene_host_command_is_bounded() {
    let start = Instant::now();
    let result = host_command::output(Command::new("sleep").arg("10"), Duration::from_millis(40));
    assert!(result.is_err());
    assert!(start.elapsed() < Duration::from_secs(2));
}
#[test]
fn disk_hygiene_shared_cache_busy_and_pinned_paths_survive() {
    let tmp = tempfile::tempdir().unwrap();
    let cache = tmp.path().join("cargo-target");
    fs::create_dir(&cache).unwrap();
    fs::write(cache.join("rebuildable"), [0u8; 42]).unwrap();
    let mut report = Report {
        free_bytes: 4_000_000_000,
        alarm: true,
        dry_run: true,
        freed_bytes: 0,
        actions: vec![],
        skipped: vec![],
        error: None,
        projected: Default::default(),
    };
    shared_cache(&cache, &Guards::default(), true, false, &mut report).unwrap();
    assert!(report.actions.is_empty());
    let guards = Guards {
        pinned: vec![cache.clone()],
        live: vec![],
    };
    shared_cache(&cache, &guards, false, true, &mut report).unwrap();
    assert!(cache.exists());
}

#[test]
fn disk_hygiene_python_terminal_states_and_survivors() {
    let tmp = tempfile::tempdir().unwrap();
    let pins = tmp.path().join("pins.json");
    fs::write(&pins, "[]").unwrap();
    let record = tmp.path().join("runs/run/record.json");
    fs::create_dir_all(record.parent().unwrap()).unwrap();
    for status in [
        "timed_out",
        "killed",
        "succeeded",
        "skipped",
        "terminated_after_answer",
    ] {
        fs::write(
            &record,
            serde_json::to_vec(&serde_json::json!({"status":status})).unwrap(),
        )
        .unwrap();
        assert!(Guards::load(&pins, tmp.path()).is_ok(), "{status}");
    }
    fs::write(
        &record,
        r#"{"status":"failed","survivors":[123],"cwd":"/protected"}"#,
    )
    .unwrap();
    let guards = Guards::load(&pins, tmp.path()).unwrap();
    assert!(guards.protects(std::path::Path::new("/protected")));
}
