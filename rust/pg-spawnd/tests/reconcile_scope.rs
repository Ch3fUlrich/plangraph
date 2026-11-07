//! Real scope survives its daemon owner and is settled once after disappearance.
#![cfg(target_os = "linux")]
mod common;
use common::{ChildGuard, TempDir};
use pg_worker_core::run::record::{RunRecord, Status};
use serde_json::json;
use std::{process::Command, time::Duration};

#[test]
fn scope_child_is_adopted_then_orphaned_once() {
    let tmp = TempDir::new();
    let state = tmp.path().join("state");
    let id = pg_worker_core::run::record::new_run_id();
    let membership = std::fs::read_to_string("/proc/self/cgroup").unwrap();
    let relative = membership
        .lines()
        .find_map(|line| line.strip_prefix(concat!("0:", ":")))
        .unwrap();
    let parent = std::path::Path::new("/sys/fs/cgroup").join(relative.trim_start_matches('/'));
    let scope = parent.join(format!("pg-{id}.scope"));
    if let Err(error) = std::fs::create_dir(&scope) {
        skip_privilege(error);
        return;
    }
    struct Scope(std::path::PathBuf);
    impl Drop for Scope {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir(&self.0);
        }
    }
    let _scope = Scope(scope.clone());
    // The shell waits outside the scope until membership is established.
    let ready = tmp.path().join("ready");
    let mut child = ChildGuard {
        child: Command::new("sh")
            .args([
                "-c",
                "while [ ! -e \"$1\" ]; do sleep 0.01; done; exec sleep 60",
                "scope-child",
            ])
            .arg(&ready)
            .spawn()
            .unwrap(),
    };
    let pid = child.child.id();
    if let Err(error) = std::fs::write(scope.join("cgroup.procs"), pid.to_string()) {
        skip_privilege(error);
        return;
    }
    std::fs::write(&ready, "ready").unwrap();
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).unwrap();
    let start: u64 = stat
        .rsplit_once(')')
        .unwrap()
        .1
        .split_whitespace()
        .nth(19)
        .unwrap()
        .parse()
        .unwrap();
    let fields = json!({"role":"implement", "started":1, "max_s":u32::MAX,
        "plan":{"first_leg":{"leg_key":"test-leg","route_key":"test-route"}},
        "proc":{"pid":pid,"start_time":start,"pgid":pid,"group":scope},"attempts":[]});
    RunRecord::create(
        &state,
        &id,
        "local",
        fields.as_object().unwrap().clone(),
        Status::Running,
        1.0,
    )
    .unwrap();
    let socket = tmp.path().join("socket");
    let before = std::fs::read(state.join("runs").join(&id).join("record.json")).unwrap();
    let events = std::fs::read(scope.join("cgroup.events")).unwrap();
    let log = tmp.path().join("shadow.log");
    let mut shadow = ChildGuard {
        child: Command::new(env!("CARGO_BIN_EXE_pg-spawnd"))
            .args(["serve", "--state"])
            .arg(&state)
            .arg("--socket")
            .arg(&socket)
            .env("PLANGRAPH_RELIABILITY_RECONCILE", "shadow")
            .stderr(std::fs::File::create(&log).unwrap())
            .spawn()
            .unwrap(),
    };
    drop(common::wait_connect(&socket, Duration::from_secs(3)));
    assert!(std::fs::read_to_string(&log)
        .unwrap()
        .contains("would_adopt"));
    assert_eq!(
        before,
        std::fs::read(state.join("runs").join(&id).join("record.json")).unwrap()
    );
    assert_eq!(events, std::fs::read(scope.join("cgroup.events")).unwrap());
    assert!(child.child.try_wait().unwrap().is_none());
    assert!(!state.join("reconcile.owner.lock").exists());
    shadow.child.kill().unwrap();
    shadow.child.wait().unwrap();
    for pass in 0..3 {
        let mut daemon = ChildGuard {
            child: Command::new(env!("CARGO_BIN_EXE_pg-spawnd"))
                .args(["serve", "--state"])
                .arg(&state)
                .arg("--socket")
                .arg(&socket)
                .env("PLANGRAPH_RELIABILITY_RECONCILE", "on")
                .spawn()
                .unwrap(),
        };
        drop(common::wait_connect(&socket, Duration::from_secs(3)));
        let record = RunRecord::read(&state, &id).unwrap();
        let events = record.get("events").unwrap().as_array().unwrap();
        assert_eq!(
            events
                .iter()
                .filter(|e| e["event"] == "RUN_REATTACHED")
                .count(),
            1
        );
        assert_eq!(
            events
                .iter()
                .filter(|e| e["event"] == "RUN_ORPHANED")
                .count(),
            usize::from(pass > 0)
        );
        assert_eq!(
            record.status(),
            Some(if pass == 0 {
                Status::Running
            } else {
                Status::Orphaned
            })
        );
        if pass == 0 {
            assert!(child.child.try_wait().unwrap().is_none());
        }
        daemon.child.kill().unwrap();
        daemon.child.wait().unwrap();
        if pass == 0 {
            child.child.kill().unwrap();
            child.child.wait().unwrap();
        }
    }
}

fn skip_privilege(error: std::io::Error) {
    assert!(
        matches!(
            error.raw_os_error(),
            Some(libc::EACCES | libc::EPERM | libc::EROFS | libc::ENOSYS)
        ),
        "unexpected cgroup failure: {error}"
    );
    eprintln!("SKIP real scope: {error}");
}
