#![cfg(target_os = "linux")]

use std::{fs, os::unix::fs::symlink, process::Command};

#[test]
fn alerts_once_per_host_for_old_live_processes_with_deleted_cwd() {
    let root = tempfile::tempdir().unwrap();
    let proc = root.path().join("proc");
    let state = root.path().join("state");
    fs::create_dir_all(proc.join("sys/kernel")).unwrap();
    fs::write(proc.join("sys/kernel/hostname"), "fixture-host\n").unwrap();
    fs::write(proc.join("uptime"), "8000.00 0.00\n").unwrap();
    let ticks = unsafe { libc::sysconf(libc::_SC_CLK_TCK) };
    for (pid, start, status, cwd) in [
        (1, 799 * ticks, 'S', "/some/worktree (deleted)"),
        (2, 800 * ticks, 'R', "/some/other worktree (deleted)"),
        (3, 801 * ticks, 'S', "/some/new worktree (deleted)"),
        (4, 1, 'S', "/some/existing worktree"),
        (5, 1, 'Z', "/some/dead worktree (deleted)"),
    ] {
        let dir = proc.join(pid.to_string());
        fs::create_dir(&dir).unwrap();
        fs::write(
            dir.join("stat"),
            format!("{pid} (name ) tricky) {status} {}{start}", "0 ".repeat(18)),
        )
        .unwrap();
        symlink(cwd, dir.join("cwd")).unwrap();
    }
    let snapshot = root.path().join("snapshot.json");
    fs::write(
        &snapshot,
        r#"{"pace":0,"budget":1,"ci_failed":0,"ci_total":0,"infra_failed":0,"review_age_hours":0,"review_rounds":0,"critical_idle_hours":0,"coding_vm_runs":0,"worker_idle":true,"host_drift":false,"closes_recent":0,"closes_previous":0}"#,
    )
    .unwrap();
    let run = || {
        let result = Command::new(env!("CARGO_BIN_EXE_flow-watchdog"))
            .args([snapshot.as_os_str(), state.as_os_str()])
            .args(["--deleted-cwd-proc"])
            .arg(&proc)
            .env_remove("PLANGRAPH_WATCHDOG_DELIVERY")
            .env_remove("PLANGRAPH_FLOW_PUSH")
            .output()
            .unwrap();
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
        let queue = fs::read_to_string(state.join("queue.json")).unwrap();
        serde_json::from_str::<Vec<serde_json::Value>>(&queue).unwrap()
    };
    let events = run();
    assert_eq!(events.len(), 1);
    assert_eq!(
        events[0]["message"],
        "FLOW-WATCHDOG: deleted-cwd fixture-host 2 processes"
    );
    fs::remove_dir_all(proc.join("1")).unwrap();
    assert_eq!(run().len(), 1);
}
