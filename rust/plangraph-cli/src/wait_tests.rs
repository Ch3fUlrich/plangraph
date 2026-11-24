use super::*;

#[test]
fn every_wait_leaves_a_result_file_a_successor_can_read() {
    let dir = tempfile::tempdir().unwrap();
    let outcome = Outcome::Timeout {
        detail: "deadline 5s: still pending: run=1 status=queued".into(),
    };
    let path = write_record(
        dir.path(),
        "ci-pr1270",
        "ci",
        "ci-pr1270",
        &outcome,
        Duration::from_secs(5),
    )
    .unwrap();
    let record: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    assert_eq!(record["outcome"], "timeout");
    assert_eq!(record["kind"], "ci");
    assert!(record["detail"].as_str().unwrap().contains("run=1"));
}

#[test]
fn resume_digest_lists_the_finished_waits_of_the_job() {
    let job = tempfile::tempdir().unwrap();
    let outcome = Outcome::Green {
        detail: "run=7 conclusion=success".into(),
    };
    write_record(
        &job.path().join("waits"),
        "ci-pr1",
        "ci",
        "ci-pr1",
        &outcome,
        Duration::from_secs(1),
    )
    .unwrap();
    let rows = crate::watchers::digest(job.path()).unwrap();
    assert_eq!(rows.len(), 1, "{rows:?}");
    assert!(
        rows[0].starts_with("wait ci-pr1: green run=7 conclusion=success"),
        "{rows:?}"
    );
}
