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
    #[ignore = "RL29 step 2 red test: invariant 3"]
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
        assert!(!reconcile(&dir).unwrap(), "populated hold, not reclaim");
        assert!(member.alive());
        assert_unresolved(&dir);
        repair_and_reclaim(&dir, &mut member, &group, identity);
    }

    #[test]
    #[ignore = "RL29 step 2 red test: invariant 2"]
    fn rl29_zombie_live_member_holds_grace_then_reclaims() {
        let dir = StateDir::new();
        let mut member = Member::new();
        let mut leader = Member::new();
        let mut identity = leader.identity();
        // Leave the child unreaped. Drop reaps it even if an assertion panics.
        assert_eq!(
            unsafe { libc::kill(leader.0.id() as i32, libc::SIGKILL) },
            0
        );
        // The killed child is our controlled event. WNOWAIT observes its exit
        // without reaping it; no polling, scheduling deadline, or elapsed clock.
        let mut info = std::mem::MaybeUninit::<libc::siginfo_t>::uninit();
        // SAFETY: info is writable; P_PID selects our child, retained by WNOWAIT.
        assert_eq!(
            unsafe {
                libc::waitid(
                    libc::P_PID,
                    leader.0.id(),
                    info.as_mut_ptr(),
                    libc::WEXITED | libc::WNOWAIT,
                )
            },
            0
        );
        let stat = std::fs::read_to_string(format!("/proc/{}/stat", leader.0.id())).unwrap();
        assert_eq!(
            stat.rsplit_once(')').unwrap().1.split_whitespace().next(),
            Some("Z")
        );
        let (_guard, group) = populated(&dir, &member, &mut identity);
        let mut record = dir.record();
        record.set("proc", identity.clone());
        record.save().unwrap();
        seed_hold(&dir);
        assert!(!reconcile(&dir).unwrap(), "live zombie-group member holds");
        assert!(member.alive());
        assert_unresolved(&dir);
        leader.0.wait().unwrap();
        repair_and_reclaim(&dir, &mut member, &group, identity);
    }

    fn repair_and_reclaim(
        dir: &StateDir,
        member: &mut Member,
        group: &std::path::Path,
        identity: Value,
    ) {
        // Test acts as cleanup owner and kernel events producer. Production
        // must not write cgroup.events; this regular-file fixture is writable.
        member.0.kill().unwrap();
        member.0.wait().unwrap();
        std::fs::write(group.join("cgroup.procs"), "").unwrap();
        std::fs::write(group.join("cgroup.events"), "populated 0\n").unwrap();
        assert!(!member.alive());
        assert!(gone("run-test", Some(&serde_json::from_value(identity).unwrap())).unwrap());
        assert!(
            reconcile(dir).unwrap(),
            "fresh empty proof permits repair reclaim"
        );
        assert_eq!(
            RunRecord::read(&dir.0, "run-test").unwrap().status(),
            Some(Status::Orphaned)
        );
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
    #[ignore = "RL29 step 2 red test: invariant 6"]
    fn rl29_stale_identity_without_confinement_proof_becomes_unresolved() {
        let dir = StateDir::new();
        let mut member = Member::new();
        stale_record(&dir, &member, 1);
        seed_hold(&dir);
        assert!(
            !reconcile(&dir).unwrap(),
            "expired uncertainty retains capacity"
        );
        assert_unresolved(&dir);
        assert!(
            !reconcile(&dir).unwrap(),
            "restart is terminal, never Unknown"
        );
        assert!(member.alive(), "never signal a reused PID");
        assert_unresolved(&dir);
        let before = saved(&dir);
        assert!(
            !reconcile(&dir).unwrap(),
            "terminal replay retains capacity"
        );
        assert_eq!(
            saved(&dir),
            before,
            "terminal replay must not duplicate the alert"
        );
    }

    #[test]
    #[ignore = "RL29 step 2 red test: invariant 5"]
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
            assert!(!reconcile(&dir).unwrap(), "re-key retains capacity");
            let hold = saved(&dir);
            // Proposed evidence fields: unlike seeded anchor values, these
            // must be written by reconcile after observing the latest record.
            assert_eq!(
                hold["observed_identity"],
                record.get("proc").unwrap().clone()
            );
            assert_eq!(hold["observed_attempt"], generation + 1);
            assert_eq!(hold["hold_started_ms"], START);
            assert_eq!(hold["hold_deadline_ms"], DEADLINE);
            assert_eq!(hold["slot_id"], "slot-test");
        }
        assert!(member.alive());
        assert_unresolved(&dir);
        let before = saved(&dir);
        assert!(!reconcile(&dir).unwrap(), "re-key replay retains capacity");
        assert_eq!(saved(&dir), before);
    }
}
