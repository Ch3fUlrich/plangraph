use super::*;
use std::os::unix::process::CommandExt;

struct Leader(std::process::Child);
impl Leader {
    fn new() -> Self {
        Self(
            std::process::Command::new("sleep")
                .arg("60")
                .process_group(0)
                .spawn()
                .unwrap(),
        )
    }
}
impl Drop for Leader {
    fn drop(&mut self) {
        self.0.kill().unwrap();
        self.0.wait().unwrap();
    }
}

#[test]
fn reused_pgid_mismatching_leader_start_time_settles() {
    let leader = Leader::new();
    let stat = std::fs::read_to_string(format!("/proc/{}/stat", leader.0.id())).unwrap();
    let start: u64 = stat
        .rsplit_once(')')
        .unwrap()
        .1
        .split_whitespace()
        .nth(19)
        .unwrap()
        .parse()
        .unwrap();
    let dir = StateDir::new();
    let mut record = dir.record();
    record.set(
        "proc",
        json!({"pid":0,"pgid":leader.0.id(),"start_time":start + 1}),
    );
    record.save().unwrap();
    let row = inventory(&dir.0).unwrap().remove(0);
    assert!(orphan_gone(&dir.0, &row).unwrap());
    assert_eq!(
        RunRecord::read(&dir.0, "run-test").unwrap().status(),
        Some(Status::Orphaned)
    );
    assert_eq!(unsafe { libc::kill(-(leader.0.id() as i32), 0) }, 0);
}

#[test]
fn live_pgid_without_start_time_settles_only_after_grace() {
    let leader = Leader::new();
    let dir = StateDir::new();
    let mut record = dir.record();
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs_f64();
    record.set("proc", json!({"pid":0,"pgid":leader.0.id()}));
    record.set("started", json!(now));
    record.save().unwrap();
    let row = inventory(&dir.0).unwrap().remove(0);
    assert!(matches!(
        orphan_gone(&dir.0, &row),
        Err(ReconcileError::Unknown)
    ));
    assert_eq!(
        RunRecord::read(&dir.0, "run-test").unwrap().status(),
        Some(Status::Running)
    );
    record.set(
        "started",
        json!(now - unknown::UNKNOWN_IDENTITY_GRACE_MS as f64 / 1000.0 - 1.0),
    );
    record.save().unwrap();
    let row = inventory(&dir.0).unwrap().remove(0);
    assert!(orphan_gone(&dir.0, &row).unwrap());
    let saved = RunRecord::read(&dir.0, "run-test").unwrap();
    assert_eq!(saved.status(), Some(Status::Orphaned));
    assert_eq!(saved.get("attempts").unwrap()[0]["how"], "unknown_identity");
    assert_eq!(unsafe { libc::kill(-(leader.0.id() as i32), 0) }, 0);
}
