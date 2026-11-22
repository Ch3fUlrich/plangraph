#![cfg(unix)]
#[path = "common/binary.rs"]
mod binary;

use pg_worker_core::worker_host::{Phase, Store};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::Path,
    process::Command,
    time::{SystemTime, UNIX_EPOCH},
};

fn script(path: &Path, contents: &str) {
    fs::write(path, format!("#!/bin/sh\nset -eu\n{contents}\n")).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
}

#[test]
fn rejoin_cli_syncs_checks_install_and_records_run_resolution() {
    let fixture = tempfile::tempdir().unwrap();
    let home = fixture.path();
    let config = home.join("plangraph");
    let repo = home.join("checkout");
    let hooks = repo.join("tools/hooks");
    let trees = repo.join("trees");
    let executables = home.join("bin");
    let state = home.join("state");
    for dir in [&config, &hooks, &trees, &executables, &state] {
        fs::create_dir_all(dir).unwrap();
    }
    let manifest = home.join("build.json");
    let stamp = home.join("source-stamp");
    let reconcile = executables.join("reconcile-fixture");
    fs::write(
        config.join("hosts.json"),
        json!({
            "version":1, "self":"fixture-worker",
            "hosts":[{
                "id":"fixture-worker", "aliases":[], "transport":"local", "os":"linux",
                "state_dir":state, "session_mb":1, "roles":["implement"],
                "caps":{"max_runs":2,"max_writers":1,"max_seats":1,"max_sessions":1,"mem_floor_mb":1},
                "warmup":{"warmup_s":1,"ramp_mb":{"implement":1}},
                "env":{
                    "PG_REJOIN_BUILD_RECEIPT":manifest,
                    "PG_REJOIN_SOURCE_STAMP":stamp,
                    "PG_REJOIN_RUN_RECONCILER":reconcile
                },
                "repos":{"plangraph":{"root":repo,"worktrees_root":trees,
                    "remote":"https://example.invalid/project","import_roots":[],"cache_env":{}}}
            }]
        })
        .to_string(),
    )
    .unwrap();
    let main = "a".repeat(40);
    let old = "b".repeat(40);
    fs::write(&stamp, &old).unwrap();
    let sha256 = format!("{:x}", Sha256::digest(fs::read(binary::spawnd()).unwrap()));
    fs::write(
        &manifest,
        json!({"main_sha":main, "main_sha256":sha256}).to_string(),
    )
    .unwrap();

    script(
        &executables.join("systemctl"),
        "test \"$*\" = '--user start plangraph-host-sync.service'\nprintf '%s' \"$FIXTURE_MAIN\" > \"$FIXTURE_REPO/head\"",
    );
    script(
        &executables.join("git"),
        r#"shift 2
case "$*" in
  "symbolic-ref --short HEAD") printf 'main\n';;
  "rev-parse --verify refs/remotes/origin/main^{commit}") printf '%s\n' "$FIXTURE_MAIN";;
  "rev-parse --verify HEAD^{commit}") cat "$FIXTURE_REPO/head";;
  "status --porcelain --untracked-files=no") ;;
  "rev-parse --path-format=absolute --git-path hooks") printf '%s/tools/hooks\n' "$FIXTURE_REPO";;
  "rev-parse --show-toplevel") printf '%s\n' "$FIXTURE_REPO";;
  "ls-remote --exit-code origin refs/heads/main") printf '%s\trefs/heads/main\n' "$FIXTURE_MAIN";;
  *) exit 1;;
esac"#,
    );
    script(
        &reconcile,
        r#"test "$*" = 'fixture-worker run-example'
printf '%s\n' '{"host":"fixture-worker","runs":[{"run_id":"run-example","outcome":"collected"}]}'"#,
    );
    script(&executables.join("pg-worktree-sweep"), "exit 0");
    script(&executables.join("python3"), "exit 0");
    fs::write(repo.join("head"), &old).unwrap();
    let run_dir = state.join("runs/run-example");
    fs::create_dir_all(&run_dir).unwrap();
    fs::write(
        run_dir.join("record.json"),
        r#"{"run_id":"run-example","host":"fixture-worker","status":"unknown"}"#,
    )
    .unwrap();

    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs();
    let store = Store::new(&state, "fixture-worker").unwrap();
    store.observe(now - 700, false, None).unwrap();
    store.observe(now - 10, true, None).unwrap();
    let invoke = || {
        Command::new(binary::spawnd())
            .args(["rejoin", "fixture-worker"])
            .env("PLANGRAPH_SPAWN_STATE", &state)
            .env("XDG_CONFIG_HOME", home)
            .env("PATH", format!("{}:/usr/bin:/bin", executables.display()))
            .env("FIXTURE_REPO", &repo)
            .env("FIXTURE_MAIN", &main)
            .output()
            .unwrap()
    };
    let stale = invoke();
    assert!(!stale.status.success());
    let error = String::from_utf8_lossy(&stale.stderr);
    assert!(error.contains("source_sha_mismatch"), "{error}");
    assert_eq!(fs::read_to_string(repo.join("head")).unwrap(), main);
    assert_eq!(store.phase().unwrap(), Phase::Rejoining);
    let record = || -> Value {
        serde_json::from_slice(&fs::read(state.join("worker-hosts/fixture-worker.json")).unwrap())
            .unwrap()
    };
    assert_eq!(
        record()["rejoin"]["completed"][1],
        json!(["runs", [{"run_id":"run-example","outcome":"collected"}]])
    );

    fs::write(stamp, main.as_bytes()).unwrap();
    let recovered = invoke();
    assert!(
        recovered.status.success(),
        "{}",
        String::from_utf8_lossy(&recovered.stderr)
    );
    assert_eq!(store.phase().unwrap(), Phase::Ready);
    assert_eq!(record()["rejoin"]["ready"], true);
}
