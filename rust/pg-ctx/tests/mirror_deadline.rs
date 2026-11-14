#![cfg(target_os = "linux")]
use pg_ctx::mirror::Mirror;
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    time::{Duration, Instant},
};
#[test]
fn hung_fetch_is_killed_and_later_repository_is_refreshed() {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path();
    let origin = root.join("origin");
    fs::create_dir(&origin).unwrap();
    let git = |args: &[&str]| {
        let out = pg_worker_core::git_test_support::command(&origin)
            .args(args)
            .output()
            .unwrap();
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
    };
    git(&["init", "--initial-branch=main"]);
    git(&["commit", "--allow-empty", "-m", "fixture"]);
    let config = root.join("repos.json");
    fs::write(
        &config,
        serde_json::to_vec(
            &serde_json::json!({"first":root.join("missing"),"second":origin.join(".git")}),
        )
        .unwrap(),
    )
    .unwrap();
    let pid = root.join("hung.pid");
    let worker = root.join("fetch-worker");
    fs::write(
        &worker,
        format!(
            r#"#!/bin/sh
case "$(cat "$2")" in
*'"repo":"first"'*) echo $$ > '{}'; exec sleep 30 ;;
*) exec '{}' "$@" ;;
esac
"#,
            pid.display(),
            env!("CARGO_BIN_EXE_pg-repo-mirror")
        ),
    )
    .unwrap();
    fs::set_permissions(&worker, fs::Permissions::from_mode(0o755)).unwrap();
    let mirror = Mirror::from_repos(&config, &root.join("mirrors"))
        .unwrap()
        .with_fetch_worker(worker);
    let start = Instant::now();
    let result = mirror.fetch_all_with_timeout(Duration::from_millis(500));
    assert!(start.elapsed() < Duration::from_secs(4));
    assert_eq!(format!("{:?}", result["first"]), "Err(FetchTimedOut)");
    assert!(result["second"].is_ok(), "{result:?}");
    assert!(mirror.watermark("second").unwrap().is_some());
    let pid = fs::read_to_string(pid).unwrap();
    assert!(
        !std::path::Path::new("/proc").join(pid.trim()).exists(),
        "timed-out child must be killed and reaped"
    );
}
