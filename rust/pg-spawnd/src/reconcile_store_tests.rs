use super::*;
use pg_worker_core::run::record::{RunRecord, Status};
use serde_json::json;

struct StateDir(std::path::PathBuf);
impl StateDir {
    fn new() -> Self {
        let path = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
        std::fs::create_dir(&path).unwrap();
        Self(path)
    }
    fn record(&self) -> RunRecord {
        RunRecord::create(
            &self.0,
            "run-test",
            "local",
            json!({
                "role":"implement", "plan":{"first_leg":{"leg_key":"leg", "route_key":"route"}}
            })
            .as_object()
            .unwrap()
            .clone(),
            Status::Running,
            1.0,
        )
        .unwrap()
    }
}
impl Drop for StateDir {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.0).unwrap();
    }
}

#[cfg(target_os = "linux")]
struct GroupChild(std::process::Child);
#[cfg(target_os = "linux")]
impl GroupChild {
    fn new() -> Self {
        use std::os::unix::process::CommandExt;
        Self(
            std::process::Command::new("sh")
                .args(["-c", "read token"])
                .stdin(std::process::Stdio::piped())
                .process_group(0)
                .spawn()
                .unwrap(),
        )
    }
    fn identity(&self) -> Value {
        let stat = std::fs::read_to_string(format!("/proc/{}/stat", self.0.id())).unwrap();
        let start: u64 = stat
            .rsplit_once(')')
            .unwrap()
            .1
            .split_whitespace()
            .nth(19)
            .unwrap()
            .parse()
            .unwrap();
        json!({"pid":self.0.id(), "start_time":start,"group":null})
    }
    fn stop(&mut self) {
        self.0.kill().unwrap();
        self.0.wait().unwrap();
    }
}
#[cfg(target_os = "linux")]
impl Drop for GroupChild {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

#[cfg(target_os = "linux")]
#[test]
fn reconcile_on_killed_group_orphaned_once_across_startups() {
    let dir = StateDir::new();
    let mut record = dir.record();
    let mut child = GroupChild::new();
    record.set("proc", child.identity());
    record.save().unwrap();
    child.stop();
    crate::reconcile::startup(&dir.0, Some("on"), &json!({})).unwrap();
    let before = std::fs::read(record.path()).unwrap();
    crate::reconcile::startup(&dir.0, Some("on"), &json!({})).unwrap();
    assert_eq!(before, std::fs::read(record.path()).unwrap());
    let value: Value = serde_json::from_slice(&before).unwrap();
    assert_eq!(value["status"], "orphaned");
    assert_eq!(value["attempts"][0]["outcome"]["outcome"], "orphaned");
    assert_eq!(value["events"].as_array().unwrap().len(), 1);
    assert_eq!(value["events"][0]["event"], "RUN_ORPHANED");
    assert_eq!(value["events"][0]["how"], "gone");
    assert!(value["exit_code"].is_null());
}

#[cfg(target_os = "linux")]
#[test]
fn reconcile_on_live_group_would_reattach_untouched_shadow_writes_nothing() {
    let dir = StateDir::new();
    let mut record = dir.record();
    let mut child = GroupChild::new();
    record.set("proc", child.identity());
    record.save().unwrap();
    let before = std::fs::read(record.path()).unwrap();
    crate::reconcile::startup(&dir.0, Some("shadow"), &json!({})).unwrap();
    assert!(!dir.0.join("reconcile.lock").exists());
    assert!(matches!(
        crate::reconcile::startup(&dir.0, Some("on"), &json!({})),
        Err(ReconcileError::Unknown)
    ));
    assert!(child.0.try_wait().unwrap().is_none());
    assert_eq!(before, std::fs::read(record.path()).unwrap());
}

#[cfg(target_os = "linux")]
#[test]
fn reconcile_on_pid_reuse_with_live_group_is_not_gone() {
    let dir = StateDir::new();
    let mut record = dir.record();
    let mut child = GroupChild::new();
    let mut identity = child.identity();
    identity["start_time"] = json!(identity["start_time"].as_u64().unwrap() + 1);
    record.set("proc", identity);
    record.save().unwrap();
    assert!(matches!(
        crate::reconcile::startup(&dir.0, Some("on"), &json!({})),
        Err(ReconcileError::Unknown)
    ));
    assert!(child.0.try_wait().unwrap().is_none());
    assert_eq!(
        RunRecord::read(&dir.0, "run-test").unwrap().status(),
        Some(Status::Running)
    );
}

#[cfg(target_os = "linux")]
#[test]
fn reconcile_named_group_members_override_reused_leader_identity() {
    let dir = StateDir::new();
    let group = dir.0.join("scope");
    std::fs::create_dir(&group).unwrap();
    std::fs::write(group.join("cgroup.procs"), "123\n").unwrap();
    let identity = Proc {
        pid: std::process::id(),
        start_time: Some(1),
        group: Some(group.to_str().unwrap().into()),
    };
    assert!(!gone(Some(&identity)).unwrap());
    std::fs::write(group.join("cgroup.procs"), "").unwrap();
    assert!(gone(Some(&identity)).unwrap());
}

#[test]
fn reconcile_inventory_legacy_missing_proc_and_limits() {
    let dir = StateDir::new();
    let mut record = dir.record();
    let rows = inventory(&dir.0).unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].attempt, 1);
    assert!(rows[0].identity.is_none());
    record.set("max_s", json!(-1));
    record.save().unwrap();
    assert!(inventory(&dir.0).is_err());
}

#[test]
fn reconcile_shadow_accepts_unknown_start_time_and_optional_group() {
    for proc in [
        json!({"pid":123,"start_time":null,"group":null}),
        json!({"pid":123}),
    ] {
        let dir = StateDir::new();
        let mut record = dir.record();
        record.set("proc", proc.clone());
        record.set("max_s", json!(7200)); // old records remain readable
        record.save().unwrap();
        assert!(crate::reconcile::startup(&dir.0, Some("shadow"), &json!({})).is_ok());
        record.set(
            "attempts",
            json!([{"status":"running", "started":1,
            "leg":{}, "proc":proc}]),
        );
        record.save().unwrap();
        assert!(crate::reconcile::startup(&dir.0, Some("shadow"), &json!({})).is_ok());
    }
}

#[test]
fn reconcile_settle_one_outcome_one_event_null_exit() {
    let dir = StateDir::new();
    let mut record = dir.record();
    record.set(
        "attempts",
        json!([{"status":"running", "started":1,
        "leg":{"leg_key":"leg", "route_key":"route"}}]),
    );
    settle(&dir.0, &mut record, 1, "ended_unobserved", 2000).unwrap();
    settle(&dir.0, &mut record, 1, "ended_unobserved", 3000).unwrap();
    assert_eq!(record.get("status").unwrap(), "orphaned");
    assert!(record.get("exit_code").unwrap().is_null());
    assert_eq!(record.get("ended").unwrap(), 2.0);
    assert_eq!(record.get("events").unwrap().as_array().unwrap().len(), 1);
    let attempts = record.get("attempts").unwrap();
    assert_eq!(attempts[0]["outcome"]["outcome"], "orphaned");
    assert_eq!(attempts[0]["outcome"]["ts"], 2);
}

#[test]
fn reconcile_inventory_ignores_terminal_and_refuses_mismatch() {
    let dir = StateDir::new();
    let mut record = dir.record();
    record.set("run_id", json!("run-other"));
    record.save().unwrap();
    assert!(inventory(&dir.0).is_err());
    record.set("run_id", json!("run-test"));
    record.transition(Status::Orphaned, 2.0).unwrap();
    record.save().unwrap();
    assert!(inventory(&dir.0).unwrap().is_empty());
}

#[cfg(unix)]
#[test]
fn reconcile_inventory_refuses_symlink_record() {
    let dir = StateDir::new();
    let record = dir.record();
    let moved = dir.0.join("saved.json");
    std::fs::rename(record.path(), &moved).unwrap();
    std::os::unix::fs::symlink(moved, record.path()).unwrap();
    assert!(inventory(&dir.0).is_err());
}

#[cfg(target_os = "linux")]
#[test]
fn reconcile_crashed_daemon_real_child_inventory_is_read_only() {
    use std::io::{BufRead, BufReader};
    use std::process::{Command, Stdio};
    let dir = StateDir::new();
    let mut record = dir.record();
    let mut daemon = Command::new("sh")
        .args([
            "-c",
            "sh -c 'exit 0' & child=$!; wait \"$child\"; echo ready; read token",
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut ready = String::new();
    BufReader::new(daemon.stdout.take().unwrap())
        .read_line(&mut ready)
        .unwrap();
    daemon.kill().unwrap();
    daemon.wait().unwrap();
    assert_eq!(ready, "ready\n");
    record.set(
        "proc",
        json!({"pid":daemon.id(),"start_time":1,"group":null}),
    );
    record.save().unwrap();
    let before = std::fs::read(record.path()).unwrap();
    let diagnostics = crate::reconcile::startup(&dir.0, Some("shadow"), &json!({})).unwrap();
    assert_eq!(
        diagnostics,
        vec![crate::reconcile::WouldOrphan {
            run_id: "run-test".into(),
            attempt: 1,
        }]
    );
    assert_eq!(before, std::fs::read(record.path()).unwrap());
    assert!(!dir.0.join("reconcile.lock").exists());
}

#[test]
fn reconcile_missing_identity_is_unknown_and_not_settled() {
    let dir = StateDir::new();
    let record = dir.record();
    let before = std::fs::read(record.path()).unwrap();
    assert!(matches!(
        crate::reconcile::startup(&dir.0, Some("on"), &json!({})),
        Err(ReconcileError::Unknown)
    ));
    assert!(
        crate::reconcile::startup(&dir.0, Some("shadow"), &json!({}))
            .unwrap()
            .is_empty()
    );
    assert_eq!(before, std::fs::read(record.path()).unwrap());
}

#[cfg(target_os = "linux")]
#[test]
fn reconcile_nonleader_live_pid_group_esrch_is_not_settled() {
    let dir = StateDir::new();
    let mut record = dir.record();
    let mut child = GroupChild(
        std::process::Command::new("sh")
            .args(["-c", "read token"])
            .stdin(std::process::Stdio::piped())
            .spawn()
            .unwrap(),
    );
    // SAFETY: only inspect group membership and signal-zero liveness.
    unsafe {
        assert_ne!(libc::getpgid(child.0.id() as i32), child.0.id() as i32);
        assert_eq!(libc::kill(-(child.0.id() as i32), 0), -1);
        assert_eq!(
            std::io::Error::last_os_error().raw_os_error(),
            Some(libc::ESRCH)
        );
    }
    record.set("proc", child.identity());
    record.save().unwrap();
    let before = std::fs::read(record.path()).unwrap();
    assert!(matches!(
        crate::reconcile::startup(&dir.0, Some("on"), &json!({})),
        Err(ReconcileError::Unknown)
    ));
    assert!(child.0.try_wait().unwrap().is_none());
    assert_eq!(before, std::fs::read(record.path()).unwrap());
}

#[cfg(target_os = "linux")]
#[test]
fn reconcile_exited_leader_live_member_is_not_gone() {
    use std::os::unix::process::CommandExt;
    let mut leader = GroupChild::new();
    let identity: Proc = serde_json::from_value(leader.identity()).unwrap();
    let mut member = GroupChild(
        std::process::Command::new("sh")
            .args(["-c", "read token"])
            .stdin(std::process::Stdio::piped())
            .process_group(leader.0.id() as i32)
            .spawn()
            .unwrap(),
    );
    leader.stop();
    assert!(!gone(Some(&identity)).unwrap());
    assert!(member.0.try_wait().unwrap().is_none());
}

#[cfg(target_os = "linux")]
#[test]
fn reconcile_zombie_leader_is_not_gone() {
    let child = GroupChild::new();
    let identity: Proc = serde_json::from_value(child.identity()).unwrap();
    // SAFETY: terminate only our child, leaving it unreaped for the probe.
    assert_eq!(unsafe { libc::kill(child.0.id() as i32, libc::SIGKILL) }, 0);
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    loop {
        let stat = std::fs::read_to_string(format!("/proc/{}/stat", child.0.id())).unwrap();
        if stat.rsplit_once(')').unwrap().1.split_whitespace().next() == Some("Z") {
            break;
        }
        assert!(std::time::Instant::now() < deadline);
        std::thread::yield_now();
    }
    assert!(!gone(Some(&identity)).unwrap());
}
