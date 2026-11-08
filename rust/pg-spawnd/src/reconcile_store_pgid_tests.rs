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
        let _ = self.0.kill();
        self.0.wait().unwrap();
    }
}

#[test]
fn every_running_identity_is_bounded_or_verified() {
    use pg_worker_core::slots;
    for state in ["live", "Z", "Z-member", "gone"] {
        let mut leader = Leader::new();
        let _member = (state == "Z-member").then(|| {
            Leader(
                std::process::Command::new("sleep")
                    .arg("60")
                    .process_group(leader.0.id() as i32)
                    .spawn()
                    .unwrap(),
            )
        });
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
        if state != "live" {
            leader.0.kill().unwrap();
            if state == "gone" {
                leader.0.wait().unwrap();
            } else {
                let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
                loop {
                    let stat =
                        std::fs::read_to_string(format!("/proc/{}/stat", leader.0.id())).unwrap();
                    if stat.rsplit_once(')').unwrap().1.split_whitespace().next() == Some("Z") {
                        break;
                    }
                    assert!(std::time::Instant::now() < deadline);
                    std::thread::yield_now();
                }
            }
        }
        let mut dead = std::process::Command::new("true").spawn().unwrap();
        dead.wait().unwrap();
        let mut counts = [0, 0];
        for pid in [0, leader.0.id(), dead.id()] {
            for pgid in [None, Some(leader.0.id()), Some(dead.id())] {
                for grouped in [false, true] {
                    for recorded in [None, Some(start), Some(start + 1)] {
                        let label =
                            format!("pid={pid} pgid={pgid:?} group={grouped} start={recorded:?}");
                        let dir = StateDir::new();
                        let (_scope_root, scope) = cgroup_tests::ScopeRoot::new(&dir.0);
                        std::fs::write(scope.join("cgroup.events"), "populated 1\n").unwrap();
                        std::fs::write(scope.join("cgroup.procs"), leader.0.id().to_string())
                            .unwrap();
                        let mut record = dir.record();
                        record.set(
                            "attempts",
                            json!([{"status":"running", "started":1,
                        "leg":{"leg_key":"leg","route_key":"route"},
                        "proc":{"pid":pid,"pgid":pgid,"start_time":recorded,
                            "group":grouped.then(|| scope.to_str().unwrap())}}]),
                        );
                        record.save().unwrap();
                        slots::with_table(&dir.0, std::time::Duration::from_secs(1), |t| {
                            t.active.push(slots::ActiveSlot {
                                run_id: "run-test".into(),
                                pid,
                                start_time: recorded.unwrap_or(0),
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
                        // Bound: first reconciliation at/after started + 10 seconds.
                        // started=1 exercises the expired bound without wall-clock sleeps.
                        let verified = state == "live"
                            && recorded == Some(start)
                            && (pid == leader.0.id() || (pid == 0 && pgid == Some(leader.0.id())));
                        let row = inventory(&dir.0).unwrap().remove(0);
                        if state.starts_with('Z')
                            && recorded == Some(start)
                            && pid == leader.0.id()
                            && pgid == Some(leader.0.id())
                            && !grouped
                        {
                            let fresh = Entry {
                                deadline_ms: Some(u64::MAX - 10_000),
                                ..Entry {
                                    run_id: row.run_id.clone(),
                                    role: row.role.clone(),
                                    attempt: row.attempt,
                                    identity: Some(Proc {
                                        pid,
                                        pgid,
                                        start_time: recorded,
                                        group: None,
                                    }),
                                    deadline_ms: None,
                                }
                            };
                            let result = orphan_gone(&dir.0, &fresh);
                            if state == "Z-member" {
                                assert!(matches!(result, Err(ReconcileError::Unknown)));
                            } else {
                                assert!(result.unwrap());
                                counts[0] += 1;
                                continue;
                            }
                        }
                        for _ in 0..if verified { 2 } else { 1 } {
                            let result = orphan_gone(&dir.0, &row);
                            if verified {
                                assert!(!matches!(result, Ok(true)), "{label}");
                            } else {
                                assert!(result.unwrap(), "{label}");
                            }
                        }
                        let saved = RunRecord::read(&dir.0, "run-test").unwrap();
                        assert_eq!(
                            saved.status(),
                            Some(if verified {
                                Status::Running
                            } else {
                                Status::Orphaned
                            }),
                            "{label}"
                        );
                        let slots =
                            slots::with_table(&dir.0, std::time::Duration::from_secs(1), |t| {
                                t.active.len()
                            })
                            .unwrap();
                        assert_eq!(slots, usize::from(verified), "{label}");
                        if !verified {
                            assert_eq!(
                                saved.get("attempts").unwrap()[0]["status"],
                                "orphaned",
                                "{label}"
                            );
                        }
                        counts[usize::from(verified)] += 1;
                    }
                }
            }
        }
        assert_eq!(counts, if state == "live" { [46, 8] } else { [54, 0] });
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
