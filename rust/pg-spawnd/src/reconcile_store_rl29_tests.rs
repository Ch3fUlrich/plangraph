//! RL29 §4 red acceptance tests. Sidecar fields are the proposed §3 contract.
use super::*;
use std::{path::PathBuf, process::Stdio};

struct Member(std::process::Child);
impl Member {
    fn new() -> Self {
        Self(
            std::process::Command::new("sh")
                .args(["-c", "read token"])
                .stdin(Stdio::piped())
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
        json!({"pid":self.0.id(), "start_time":start,"group":null,"pgid":null})
    }
    fn alive(&mut self) -> bool {
        self.0.try_wait().unwrap().is_none()
    }
}
impl Drop for Member {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

// Inject the persisted clock input, not a wall-clock sleep. Both timestamps are
// already overdue on every supported host; restart must consume this same budget.
const START: u64 = 1_000;
const DEADLINE: u64 = START + 10_000;
fn sidecar(dir: &StateDir) -> PathBuf {
    dir.0.join("runs/run-test/reconcile.json")
}
fn seed_hold(dir: &StateDir) {
    std::fs::write(
        sidecar(dir),
        serde_json::to_vec(&json!({
            "run_id":"run-test", "slot_id":"slot-test",
            "hold_started_ms":START, "hold_deadline_ms":DEADLINE,
            "effective_now_ms":DEADLINE, "reason":"unproven", "disposition":"hold",
            "reservation":"retained", "outbox":[]
        }))
        .unwrap(),
    )
    .unwrap();
}
fn saved(dir: &StateDir) -> Value {
    serde_json::from_slice(&std::fs::read(sidecar(dir)).unwrap()).unwrap()
}
fn assert_unresolved(dir: &StateDir) {
    let hold = saved(dir);
    assert_eq!(
        hold["disposition"], "unresolved",
        "expired unproved group must become terminal unresolved"
    );
    assert_eq!(hold["reservation"], "retained");
    assert_eq!(hold["hold_started_ms"], START);
    assert_eq!(hold["hold_deadline_ms"], DEADLINE);
    let alerts: Vec<_> = hold["outbox"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|event| event["event"] == "RECONCILE_UNRESOLVED")
        .collect();
    assert_eq!(
        alerts.len(),
        1,
        "one durable alert per stable run/slot hold"
    );
    assert_eq!(
        alerts[0]["idempotency_key"],
        json!(["run-test", "slot-test", START])
    );
    assert_eq!(
        RunRecord::read(&dir.0, "run-test").unwrap().status(),
        Some(Status::Running)
    );
}
fn reconcile(dir: &StateDir) -> Result<bool, ReconcileError> {
    // Reopen inventory on every call, including simulated daemon restarts.
    let row = inventory(&dir.0).unwrap().remove(0);
    orphan_gone(&dir.0, &row)
}

mod reconcile_store_settle_probe_tests {
    use super::*;

    fn populated(
        dir: &StateDir,
        member: &Member,
        identity: &mut Value,
    ) -> (super::super::cgroup_tests::ScopeRoot, PathBuf) {
        let (guard, group) = super::super::cgroup_tests::ScopeRoot::new(&dir.0);
        std::fs::write(group.join("cgroup.events"), "populated 1\n").unwrap();
        std::fs::write(group.join("cgroup.procs"), format!("{}\n", member.0.id())).unwrap();
        identity["group"] = json!(group);
        (guard, group)
    }

    #[test]
    #[ignore = "RL29 step 3: invariant 3"]
    fn rl29_dead_leader_populated_saved_group_reclaims_after_grace() {
        let dir = StateDir::new();
        let mut member = Member::new();
        let mut leader = Member::new();
        let mut identity = leader.identity();
        leader.0.kill().unwrap();
        leader.0.wait().unwrap();
        let (_guard, group) = populated(&dir, &member, &mut identity);
        let mut record = dir.record();
        record.set("proc", identity.clone());
        record.save().unwrap();
        seed_hold(&dir);
        let result = reconcile(&dir);
        if matches!(result, Ok(true)) {
            assert!(
                !member.alive(),
                "P5 reclaim requires member exit before capacity release"
            );
            assert!(std::fs::read_to_string(group.join("cgroup.events"))
                .unwrap()
                .contains("populated 0"));
            assert!(gone("run-test", Some(&serde_json::from_value(identity).unwrap())).unwrap());
        } else {
            assert!(member.alive());
            assert_unresolved(&dir);
        }
    }

    #[test]
    #[ignore = "RL29 step 3: invariant 2"]
    fn rl29_zombie_live_member_holds_grace_then_reclaims() {
        let dir = StateDir::new();
        let mut member = Member::new();
        let leader = Member::new();
        let mut identity = leader.identity();
        // Leave the child unreaped. Drop reaps it even if an assertion panics.
        assert_eq!(
            unsafe { libc::kill(leader.0.id() as i32, libc::SIGKILL) },
            0
        );
        let limit = std::time::Instant::now() + std::time::Duration::from_secs(2);
        loop {
            let stat = std::fs::read_to_string(format!("/proc/{}/stat", leader.0.id())).unwrap();
            if stat.rsplit_once(')').unwrap().1.split_whitespace().next() == Some("Z") {
                break;
            }
            assert!(std::time::Instant::now() < limit);
            std::thread::yield_now();
        }
        let (_guard, group) = populated(&dir, &member, &mut identity);
        let mut record = dir.record();
        record.set("proc", identity.clone());
        record.save().unwrap();
        seed_hold(&dir);
        if matches!(reconcile(&dir), Ok(true)) {
            assert!(
                !member.alive(),
                "orphan_gone Ok(true) must not leave a live zombie-group member"
            );
            assert!(std::fs::read_to_string(group.join("cgroup.events"))
                .unwrap()
                .contains("populated 0"));
            assert!(gone("run-test", Some(&serde_json::from_value(identity).unwrap())).unwrap());
        } else {
            assert!(member.alive());
            assert_unresolved(&dir);
        }
    }
}

mod reconcile_store_identity_tests {
    use super::*;
    fn stale_record(dir: &StateDir, member: &Member, generation: u64) {
        let mut record = if dir.0.join("runs/run-test/record.json").exists() {
            RunRecord::read(&dir.0, "run-test").unwrap()
        } else {
            dir.record()
        };
        let mut identity = member.identity();
        identity["start_time"] = json!(identity["start_time"].as_u64().unwrap() + generation);
        // A reused live PID without any authenticated confinement authority.
        record.set("proc", identity);
        record.save().unwrap();
    }

    #[test]
    #[ignore = "RL29 step 3: invariant 6"]
    fn rl29_stale_identity_without_confinement_proof_becomes_unresolved() {
        let dir = StateDir::new();
        let mut member = Member::new();
        stale_record(&dir, &member, 1);
        seed_hold(&dir);
        let _ = reconcile(&dir);
        let result = reconcile(&dir); // restart across persisted deadline
        assert!(member.alive(), "never signal a reused PID");
        assert_unresolved(&dir);
        assert!(
            !matches!(result, Err(ReconcileError::Unknown)),
            "no endless Unknown after max age"
        );
        let before = saved(&dir);
        let _ = reconcile(&dir);
        assert_eq!(
            saved(&dir),
            before,
            "terminal replay must not duplicate the alert"
        );
    }

    #[test]
    #[ignore = "RL29 step 3: invariant 5"]
    fn rl29_identity_rekey_preserves_run_slot_hold_deadline() {
        let dir = StateDir::new();
        let mut member = Member::new();
        stale_record(&dir, &member, 1);
        seed_hold(&dir);
        for generation in 1..=3 {
            stale_record(&dir, &member, generation);
            let mut record = RunRecord::read(&dir.0, "run-test").unwrap();
            let mut attempts = vec![json!({"status":"orphaned"}); generation as usize];
            attempts.push(json!({"status":"running", "started":1,
                "leg":{"leg_key":"leg","route_key":"route"}, "proc":record.get("proc")}));
            record.set("attempts", json!(attempts));
            record.save().unwrap();
            let _ = reconcile(&dir);
            let hold = saved(&dir);
            assert_eq!(hold["hold_started_ms"], START);
            assert_eq!(hold["hold_deadline_ms"], DEADLINE);
            assert_eq!(hold["slot_id"], "slot-test");
        }
        assert!(member.alive());
        assert_unresolved(&dir);
        let before = saved(&dir);
        let _ = reconcile(&dir);
        assert_eq!(saved(&dir), before);
    }
}
