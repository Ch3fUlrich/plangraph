//! Exercise reconciliation through the actual daemon startup path.
#![cfg(unix)]
mod common;
use common::{ChildGuard, TempDir};
use std::{
    process::Command,
    time::{Duration, Instant},
};

#[cfg(target_os = "linux")]
#[test]
fn reconcile_real_child_restart_locked_before_reclaim() {
    use pg_worker_core::{
        run::record::{RunRecord, Status},
        slots,
    };
    use serde_json::json;
    use std::os::unix::process::CommandExt;
    for live in [true, false] {
        let tmp = TempDir::new();
        let state = tmp.path().join("state");
        std::fs::create_dir(&state).unwrap();
        let mut child = ChildGuard {
            child: Command::new("sleep")
                .arg("60")
                .process_group(0)
                .spawn()
                .unwrap(),
        };
        let pid = child.child.id();
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
        let id = pg_worker_core::run::record::new_run_id();
        let leg = json!({"leg_key":"test-leg", "route_key":"test-route"});
        let fields = json!({"role":"implement", "started":1, "max_s":u32::MAX,
            "plan":{"first_leg":leg}, "proc":{"pid":pid,"start_time":start,"pgid":pid},
            "attempts":[]});
        RunRecord::create(
            &state,
            &id,
            "local",
            fields.as_object().unwrap().clone(),
            Status::Running,
            1.0,
        )
        .unwrap();
        slots::with_table(&state, Duration::from_secs(1), |table| {
            table.active.push(slots::ActiveSlot {
                run_id: id.clone(),
                pid,
                start_time: start,
                role: pg_worker_core::hosts::Role::Implement,
                class: slots::SlotClass::Other,
                repo_id: "test".into(),
                seat_slot: None,
                group: None,
                reservation_mb: 0,
                created_ms: 0,
                bound: true,
            });
        })
        .unwrap();
        // A real daemon stand-in is killed, not gracefully shut down.
        let mut old = ChildGuard {
            child: Command::new("sleep").arg("60").spawn().unwrap(),
        };
        if !live {
            child.child.kill().unwrap();
            child.child.wait().unwrap();
        }
        old.child.kill().unwrap();
        assert!(!old.child.wait().unwrap().success());
        let socket = tmp.path().join("socket");
        let start_daemon = || ChildGuard {
            child: Command::new(env!("CARGO_BIN_EXE_pg-spawnd"))
                .args(["serve", "--state"])
                .arg(&state)
                .arg("--socket")
                .arg(&socket)
                .env("PLANGRAPH_RELIABILITY_RECONCILE", "on")
                .spawn()
                .unwrap(),
        };
        // Block reconciliation: listener and reclaim must both remain blocked.
        let lock = pg_worker_core::lockfile::try_lock(&state.join("reconcile.lock"))
            .unwrap()
            .unwrap();
        let mut blocked = start_daemon();
        let deadline = Instant::now() + Duration::from_secs(2);
        while blocked.child.try_wait().unwrap().is_none() {
            assert!(!socket.exists());
            assert!(Instant::now() < deadline);
            std::thread::yield_now();
        }
        slots::with_table(&state, Duration::from_secs(1), |t| {
            assert_eq!(t.active.len(), 1)
        })
        .unwrap();
        drop(lock);
        for _ in 0..2 {
            let mut daemon = start_daemon();
            drop(common::wait_connect(&socket, Duration::from_secs(3)));
            let record = RunRecord::read(&state, &id).unwrap();
            assert_eq!(
                record.status(),
                Some(if live {
                    Status::Running
                } else {
                    Status::Orphaned
                })
            );
            let events = record.get("events").unwrap().as_array().unwrap();
            let kind = if live {
                "RUN_REATTACHED"
            } else {
                "RUN_ORPHANED"
            };
            assert_eq!(events.iter().filter(|e| e["event"] == kind).count(), 1);
            slots::with_table(&state, Duration::from_secs(1), |t| {
                assert_eq!(t.active.len(), usize::from(live))
            })
            .unwrap();
            if live {
                assert!(child.child.try_wait().unwrap().is_none());
            }
            daemon.child.kill().unwrap();
            daemon.child.wait().unwrap();
        }
        if live {
            child.child.kill().unwrap();
            child.child.wait().unwrap();
        }
    }
}

#[test]
fn configured_reconcile_through_serve() {
    for config in [
        "{not json",
        r#"{"reconcile":true}"#,
        r#"{"reconcile":"on"}"#,
        r#"{"reconcile":"shadow"}"#,
        r#"{"roles":{"review":{"reconcile":"shadow"}}}"#,
    ] {
        for explicit_on in [false, true] {
            let tmp = TempDir::new();
            let config_dir = tmp.path().join("plangraph");
            std::fs::create_dir(&config_dir).unwrap();
            std::fs::write(config_dir.join("reliability.json"), config).unwrap();
            let socket = tmp.path().join("socket");
            let state = tmp.path().join("state");
            std::fs::create_dir_all(state.join("runs/not-a-run-id")).unwrap();
            let mut command = Command::new(env!("CARGO_BIN_EXE_pg-spawnd"));
            command
                .arg("serve")
                .arg("--socket")
                .arg(&socket)
                .arg("--state")
                .arg(&state)
                .env("XDG_CONFIG_HOME", tmp.path())
                .env_remove("PLANGRAPH_RELIABILITY_RECONCILE");
            if explicit_on {
                command.env("PLANGRAPH_RELIABILITY_RECONCILE", "on");
            }
            let mut guard = ChildGuard {
                child: command.spawn().unwrap(),
            };
            let deadline = Instant::now() + Duration::from_secs(2);
            loop {
                if let Some(status) = guard.child.try_wait().unwrap() {
                    assert!(explicit_on, "advisory config refused {config}");
                    assert_eq!(status.code(), Some(2));
                    assert!(!socket.exists());
                    break;
                }
                if !explicit_on && socket.exists() {
                    break;
                }
                assert!(
                    Instant::now() < deadline,
                    "serve startup deadline (on={explicit_on})"
                );
                std::thread::sleep(Duration::from_millis(10));
            }
        }
    }
}
