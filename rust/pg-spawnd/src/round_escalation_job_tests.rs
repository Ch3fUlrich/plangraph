use super::*;
use crate::round_escalation_job::{brief, enqueue, ESCALATED};

const PR: &str = "https://forge/p/1";

#[test]
fn a_stall_job_plans_on_the_strongest_leg_and_leaves_the_pr_open() {
    let dir = tempfile::tempdir().unwrap();
    assert!(enqueue(dir.path(), "p:I", PR, 1).ok().unwrap());
    assert!(!enqueue(dir.path(), "p:I", PR, 1).ok().unwrap());
    let ports = Fixture::default();
    assert_eq!(run_pending(dir.path(), &ports)[0].1, "done");
    assert_eq!((ports.count("close"), ports.count("define")), (0, 0));
    let routed = ports
        .calls
        .borrow()
        .iter()
        .find(|c| c.starts_with("route"))
        .cloned();
    assert_eq!(
        routed.unwrap(),
        "route REPLAN p:I escalated | PR https://forge/p/1 kept open | slices p:I-S1 p:I-S2"
    );
    let job = load(
        &std::fs::read_dir(job_dir(dir.path()))
            .unwrap()
            .filter_map(|e| e.ok())
            .map(|e| e.path())
            .find(|p| p.extension().is_some_and(|x| x == "json"))
            .unwrap(),
    )
    .ok()
    .unwrap();
    assert_eq!(
        (job["plan"]["leg"].clone(), job["reason"].clone()),
        (json!("strong"), json!(ESCALATED))
    );
    assert_eq!(
        (job["closed"].clone(), job["defined"].clone()),
        (json!(false), json!([]))
    );
}

#[test]
fn a_replan_of_a_lockfile_only_diff_still_hands_the_open_findings_to_the_writer() {
    let dir = tempfile::tempdir().unwrap();
    enqueue(dir.path(), "p:I", PR, 1).ok().unwrap();
    let ports = Fixture::default();
    *ports.lock_only.borrow_mut() = true;
    assert_eq!(run_pending(dir.path(), &ports)[0].1, "done");
    let text = brief(dir.path(), "p:I", PR, 1).unwrap();
    assert!(text.contains("Open finding: HIGH a/one.rs:3 keep\nverbatim"));
    assert!(text.contains("MED b/three.rs: two"));
}
