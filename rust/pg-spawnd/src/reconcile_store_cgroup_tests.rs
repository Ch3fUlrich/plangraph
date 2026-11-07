use super::*;

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
        // SAFETY: read-only query of our child's actual process group.
        let pgid = unsafe { libc::getpgid(self.0.id() as i32) };
        assert!(pgid > 0);
        json!({"pid":self.0.id(), "start_time":start,"group":null,"pgid":pgid})
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
fn reconcile_removed_scope_requires_recorded_group_gone() {
    let dir = StateDir::new();
    let scope = dir.0.join("scope");
    std::fs::create_dir(&scope).unwrap();
    let mut child = GroupChild::new();
    let mut identity = child.identity();
    identity["group"] = json!(scope);
    // Simulate a reused leader so the live PGID, not the PID, is probed.
    identity["start_time"] = json!(identity["start_time"].as_u64().unwrap() + 1);
    let mut record = dir.record();
    record.set("proc", identity.clone());
    record.save().unwrap();
    let before = std::fs::read(record.path()).unwrap();
    std::fs::remove_dir(&scope).unwrap();
    assert!(matches!(
        gone(Some(&serde_json::from_value(identity.clone()).unwrap())),
        Err(ReconcileError::Unknown)
    ));
    assert!(matches!(
        crate::reconcile::startup(&dir.0, Some("on"), &json!({})),
        Err(ReconcileError::Unknown)
    ));
    assert_eq!(before, std::fs::read(record.path()).unwrap());
    child.stop();
    // Existing directory with missing events, and an EISDIR read error,
    // remain Unknown even though the recorded PGID now returns ESRCH.
    std::fs::create_dir(&scope).unwrap();
    for events_directory in [false, true] {
        if events_directory {
            std::fs::create_dir(scope.join("cgroup.events")).unwrap();
        }
        assert!(matches!(
            gone(Some(&serde_json::from_value(identity.clone()).unwrap())),
            Err(ReconcileError::Unknown)
        ));
    }
    std::fs::remove_dir(scope.join("cgroup.events")).unwrap();
    std::fs::remove_dir(&scope).unwrap();
    crate::reconcile::startup(&dir.0, Some("on"), &json!({})).unwrap();
    let settled = std::fs::read(record.path()).unwrap();
    let value: Value = serde_json::from_slice(&settled).unwrap();
    assert_eq!(value["status"], "orphaned");
    assert_eq!(value["events"].as_array().unwrap().len(), 1);
    crate::reconcile::startup(&dir.0, Some("on"), &json!({})).unwrap();
    assert_eq!(settled, std::fs::read(record.path()).unwrap());
}

#[cfg(target_os = "linux")]
#[test]
fn reconcile_removed_scope_without_recorded_pgid_unknown_untouched() {
    let dir = StateDir::new();
    let scope = dir.0.join("scope");
    std::fs::create_dir(&scope).unwrap();
    let child = GroupChild::new();
    let mut identity = child.identity();
    identity["group"] = json!(scope);
    identity["start_time"] = json!(identity["start_time"].as_u64().unwrap() + 1);
    identity.as_object_mut().unwrap().remove("pgid");
    let mut record = dir.record();
    record.set("proc", identity.clone());
    record.save().unwrap();
    let before = std::fs::read(record.path()).unwrap();
    std::fs::remove_dir(&scope).unwrap();
    assert!(matches!(
        gone(Some(&serde_json::from_value(identity).unwrap())),
        Err(ReconcileError::Unknown)
    ));
    assert!(matches!(
        crate::reconcile::startup(&dir.0, Some("on"), &json!({})),
        Err(ReconcileError::Unknown)
    ));
    assert_eq!(before, std::fs::read(record.path()).unwrap());
}

#[cfg(target_os = "linux")]
#[test]
fn reconcile_on_live_group_unknown_untouched_shadow_writes_nothing() {
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
        pgid: None,
    };
    std::fs::write(group.join("cgroup.events"), "populated 1\nfrozen 0\n").unwrap();
    assert!(!gone(Some(&identity)).unwrap());
    std::fs::write(group.join("cgroup.procs"), "").unwrap();
    let child = group.join("child");
    std::fs::create_dir(&child).unwrap();
    std::fs::write(child.join("cgroup.threads"), "123\n").unwrap();
    assert!(!gone(Some(&identity)).unwrap());
    std::fs::write(group.join("cgroup.events"), "populated 0\nfrozen 0\n").unwrap();
    assert!(gone(Some(&identity)).unwrap());
    std::fs::remove_file(group.join("cgroup.events")).unwrap();
    assert!(matches!(
        gone(Some(&identity)),
        Err(ReconcileError::Unknown)
    ));
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
    let mut legacy = child.identity();
    let recorded: Proc = serde_json::from_value(legacy.clone()).unwrap();
    legacy.as_object_mut().unwrap().remove("pgid");
    record.set("proc", legacy);
    record.save().unwrap();
    let before = std::fs::read(record.path()).unwrap();
    child.stop();
    assert!(!std::path::Path::new(&format!("/proc/{}", child.0.id())).exists());
    assert!(!gone(Some(&recorded)).unwrap());
    let legacy: Proc = serde_json::from_value(record.get("proc").unwrap().clone()).unwrap();
    assert!(matches!(gone(Some(&legacy)), Err(ReconcileError::Unknown)));
    assert!(matches!(
        crate::reconcile::startup(&dir.0, Some("on"), &json!({})),
        Err(ReconcileError::Unknown)
    ));
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
    assert!(!std::path::Path::new(&format!("/proc/{}", leader.0.id())).exists());
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
