use super::*;

#[test]
fn zombie_live_member_holds_grace_then_reclaims() {
    use std::os::unix::process::CommandExt;
    let dir = StateDir::new();
    let leader = GroupChild::new();
    let identity = leader.identity();
    let mut member = GroupChild(
        std::process::Command::new("sh")
            .args(["-c", "read token"])
            .stdin(std::process::Stdio::piped())
            .process_group(leader.0.id() as i32)
            .spawn()
            .unwrap(),
    );
    let mut record = dir.record();
    record.set("proc", identity);
    record.save().unwrap();
    let mut row = inventory(&dir.0).unwrap().remove(0);
    // SAFETY: kill only our owned child and leave it unreaped for the Z probe.
    assert_eq!(
        unsafe { libc::kill(leader.0.id() as i32, libc::SIGKILL) },
        0
    );
    let limit = std::time::Instant::now() + std::time::Duration::from_secs(5);
    while !unknown::zombie(row.identity.as_ref().unwrap()) {
        assert!(std::time::Instant::now() < limit);
        std::thread::yield_now();
    }
    assert!(unknown::needs_grace(&row));
    assert!(!gone(&row.run_id, row.identity.as_ref()).unwrap());
    row.deadline_ms = Some(u64::MAX - unknown::UNKNOWN_IDENTITY_GRACE_MS);
    assert!(matches!(
        orphan_gone(&dir.0, &row),
        Err(ReconcileError::Unknown)
    ));
    row.deadline_ms = Some(1);
    assert!(orphan_gone(&dir.0, &row).unwrap());
    assert!(member.0.try_wait().unwrap().is_none());
    assert_eq!(
        RunRecord::read(&dir.0, "run-test").unwrap().status(),
        Some(Status::Orphaned)
    );
}

#[test]
fn dead_leader_populated_saved_group_reclaims_after_grace() {
    let dir = StateDir::new();
    let (_root, scope) = ScopeRoot::new(&dir.0);
    let mut leader = GroupChild::new();
    let mut identity = leader.identity();
    identity["group"] = json!(scope);
    let mut record = dir.record();
    record.set("proc", identity);
    record.save().unwrap();
    let mut row = inventory(&dir.0).unwrap().remove(0);
    leader.stop();
    std::fs::write(scope.join("cgroup.procs"), std::process::id().to_string()).unwrap();
    std::fs::write(scope.join("cgroup.events"), "populated 1\n").unwrap();
    assert!(matches!(
        gone(&row.run_id, row.identity.as_ref()),
        Err(ReconcileError::Unknown)
    ));
    row.deadline_ms = Some(u64::MAX - unknown::UNKNOWN_IDENTITY_GRACE_MS);
    assert!(matches!(
        orphan_gone(&dir.0, &row),
        Err(ReconcileError::Unknown)
    ));
    row.deadline_ms = Some(1);
    assert!(orphan_gone(&dir.0, &row).unwrap());
    assert_eq!(
        RunRecord::read(&dir.0, "run-test").unwrap().status(),
        Some(Status::Orphaned)
    );
}
