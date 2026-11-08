//! Crash recovery with a real daemon and a shell launch adapter.
#![cfg(target_os = "linux")]
mod common;
#[path = "../src/reconcile_store_lock.rs"]
mod state_lock;
use common::{ChildGuard, McpClient, TempDir};
use pg_worker_core::{run::record::RunRecord, slots};
use serde_json::{json, Value};
use std::{
    os::unix::{fs::PermissionsExt, process::ExitStatusExt},
    process::Command,
    time::{Duration, Instant},
};

fn until(mut condition: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(10);
    while !condition() {
        assert!(Instant::now() < deadline, "bounded recovery wait expired");
        std::thread::sleep(Duration::from_millis(10));
    }
}

fn signal(pid: u32, signal: i32) {
    // SAFETY: signal only the daemon owned by this test.
    assert_eq!(unsafe { libc::kill(pid as i32, signal) }, 0);
}

struct GroupGuard(std::process::Child);
impl Drop for GroupGuard {
    fn drop(&mut self) {
        unsafe { libc::kill(-(self.0.id() as i32), libc::SIGKILL) };
        let _ = self.0.wait();
    }
}

#[test]
fn panic_guard_kills_owned_process_group() {
    use std::os::unix::process::CommandExt;
    let mut pid = 0;
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let child = Command::new("sleep")
            .arg("60")
            .process_group(0)
            .spawn()
            .unwrap();
        pid = child.id();
        let _guard = GroupGuard(child);
        assert_eq!(unsafe { libc::kill(-(pid as i32), 0) }, 0);
        panic!("exercise process-group cleanup during test unwind");
    }));
    assert!(result.is_err());
    assert_eq!(unsafe { libc::kill(-(pid as i32), 0) }, -1);
    assert_eq!(
        std::io::Error::last_os_error().raw_os_error(),
        Some(libc::ESRCH)
    );
    assert!(!std::path::Path::new(&format!("/proc/{pid}")).exists());
}

#[test]
fn killed_daemon_exited_shell_reconciles_before_reclaim_exactly_once() {
    // Subreaper is process-wide; the panic test reaps only its owned child.
    // Adopt and reap the exited shell after killing its stopped parent, rather
    // than relying on the host's PID 1 to reap promptly (not true in containers).
    assert_eq!(unsafe { libc::prctl(libc::PR_SET_CHILD_SUBREAPER, 1) }, 0);
    let tmp = TempDir::new();
    let state = tmp.path();
    let socket = state.join("socket");
    let leg = json!({"id":"leg", "harness":"sh", "model":"fake", "family":"fake",
        "leg_key":"sh/fake", "route_key":"crash-test"});
    let spec = json!({"argv":["/bin/sh","-c",
        "n=0; while [ ! -f release ] && [ $n -lt 60000 ]; do n=$((n+1)); sleep 0.01; done; exit 0"],
        "cwd":state,"env":{"PROMPT":"test"},"stdin":null,"prompt":"test"});
    for (name, body) in [
        (
            "route",
            format!("printf '%s' '{}'", json!({"legs":[],"first_leg":leg})),
        ),
        ("launch", format!("cat >/dev/null\nprintf '%s' '{spec}'")),
    ] {
        let path = state.join(name);
        std::fs::write(&path, format!("#!/bin/sh\n{body}\n")).unwrap();
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755)).unwrap();
    }
    let start = || ChildGuard {
        child: Command::new(env!("CARGO_BIN_EXE_pg-spawnd"))
            .args(["serve", "--no-scope", "--state"])
            .arg(state)
            .arg("--socket")
            .arg(&socket)
            .env("XDG_CONFIG_HOME", state.join("config"))
            .env("PLANGRAPH_RELIABILITY_RECONCILE", "on")
            .env("PG_SPAWND_ADAPTER", state.join("route"))
            .env("PG_SPAWND_LAUNCH_CMD", state.join("launch"))
            .spawn()
            .unwrap(),
    };
    let mut daemon = start();
    let mut client = McpClient::connect(&socket);
    client.initialize();
    let reply = client.request(
        "tools/call",
        json!({"name":"plangraph_agent_spawn",
        "arguments":{"prompt":"test","card":{"x":1},"wait_seconds":0}}),
    );
    let payload: Value = serde_json::from_str(
        reply["result"]["content"][0]["text"]
            .as_str()
            .unwrap_or_else(|| panic!("{reply}")),
    )
    .unwrap();
    let id = payload["run_id"].as_str().unwrap();
    until(|| {
        RunRecord::read(state, id).is_ok_and(|r| r.get("proc").is_some_and(|p| p["pid"].is_u64()))
    });
    let record = RunRecord::read(state, id).unwrap();
    let pid = record.get("proc").unwrap()["pid"].as_u64().unwrap() as i32;
    // The local fake adapter does not perform fleet admission; seed the
    // durable fleet slot with the actual launched process identity.
    slots::with_table(state, Duration::from_secs(1), |t| {
        t.active.push(slots::ActiveSlot {
            run_id: id.into(),
            pid: pid as u32,
            start_time: record.get("proc").unwrap()["start_time"].as_u64().unwrap(),
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
    signal(daemon.child.id(), libc::SIGSTOP);
    until(|| {
        std::fs::read_to_string(format!("/proc/{}/stat", daemon.child.id()))
            .unwrap()
            .rsplit_once(')')
            .unwrap()
            .1
            .trim_start()
            .starts_with('T')
    });
    assert!(
        !std::fs::read_to_string(format!("/proc/{pid}/stat"))
            .unwrap()
            .rsplit_once(')')
            .unwrap()
            .1
            .trim_start()
            .starts_with('Z'),
        "shell must still be running before release/crash"
    );
    std::fs::write(state.join("release"), "").unwrap();
    // Zombie means the shell has exited, but the stopped daemon cannot observe
    // completion or persist a terminal record before the crash.
    until(|| {
        std::fs::read_to_string(format!("/proc/{pid}/stat"))
            .unwrap()
            .rsplit_once(')')
            .unwrap()
            .1
            .trim_start()
            .starts_with('Z')
    });
    assert_eq!(
        RunRecord::read(state, id).unwrap().get("status"),
        Some(&json!("running"))
    );
    signal(daemon.child.id(), libc::SIGKILL);
    until(|| daemon.child.try_wait().unwrap().is_some());
    assert_eq!(
        daemon.child.try_wait().unwrap().unwrap().signal(),
        Some(libc::SIGKILL)
    );
    until(|| unsafe { libc::waitpid(pid, std::ptr::null_mut(), libc::WNOHANG) } == pid);
    assert!(!std::path::Path::new(&format!("/proc/{pid}")).exists());
    drop(client);
    let slots_count =
        || slots::with_table(state, Duration::from_secs(1), |t| t.active.len()).unwrap();
    assert_eq!(slots_count(), 1);
    let lock_path = state_lock::path(state);
    let lock = pg_worker_core::lockfile::try_lock(&lock_path)
        .unwrap()
        .unwrap();
    use std::os::fd::{FromRawFd, OwnedFd};
    // Observe the actual production lock open, even though failed flock closes it.
    let fd = unsafe { libc::inotify_init1(libc::IN_NONBLOCK | libc::IN_CLOEXEC) };
    assert!(fd >= 0);
    let _watch = unsafe { OwnedFd::from_raw_fd(fd) };
    let name = std::ffi::CString::new(lock_path.as_os_str().as_encoded_bytes()).unwrap();
    assert!(unsafe { libc::inotify_add_watch(fd, name.as_ptr(), libc::IN_OPEN) } >= 0);
    std::fs::remove_file(&socket).unwrap();
    let mut blocked = start();
    // The daemon opens this inode only when it attempts the production lock.
    until(|| {
        assert!(blocked.child.try_wait().unwrap().is_none());
        let mut event = [0u8; 4096];
        let read = unsafe { libc::read(fd, event.as_mut_ptr().cast(), event.len()) };
        if read < 0 {
            assert_eq!(
                std::io::Error::last_os_error().kind(),
                std::io::ErrorKind::WouldBlock
            );
        }
        read > 0
    });
    assert!(!socket.exists());
    assert_eq!(slots_count(), 1, "reclaim must wait for reconciliation");
    assert_eq!(
        RunRecord::read(state, id).unwrap().get("status"),
        Some(&json!("running"))
    );
    drop(lock);
    // serve.rs:68 runs configured recovery before bind_listener at :82;
    // reconcile_serve.rs:203-213 takes the lock before inventory, and :335-342
    // reclaims before returning. Thus connection is a completion marker.
    until(|| socket.exists());
    drop(common::wait_connect(&socket, Duration::from_secs(10)));
    assert!(blocked.child.try_wait().unwrap().is_none());
    assert_eq!(slots_count(), 0);
    blocked.child.kill().unwrap();
    until(|| blocked.child.try_wait().unwrap().is_some());
    std::fs::remove_file(&socket).unwrap();
    let mut settled = None;
    for _ in 0..2 {
        let mut restarted = start();
        until(|| socket.exists());
        drop(common::wait_connect(&socket, Duration::from_secs(10)));
        assert!(restarted.child.try_wait().unwrap().is_none());
        let record = RunRecord::read(state, id).unwrap();
        assert_eq!(record.get("status"), Some(&json!("orphaned")));
        let attempts = record.get("attempts").unwrap().as_array().unwrap();
        assert_eq!(
            attempts
                .iter()
                .filter(|a| a["outcome"]["outcome"] == "orphaned")
                .count(),
            1
        );
        let events = record.get("events").unwrap().as_array().unwrap();
        assert_eq!(
            events
                .iter()
                .filter(|e| e["event"] == "RUN_ORPHANED")
                .count(),
            1
        );
        assert_eq!(slots_count(), 0);
        let bytes = std::fs::read(record.path()).unwrap();
        if let Some(previous) = &settled {
            assert_eq!(previous, &bytes);
        }
        settled = Some(bytes);
        restarted.child.kill().unwrap();
        until(|| restarted.child.try_wait().unwrap().is_some());
        std::fs::remove_file(&socket).unwrap();
    }
}
