use super::*;
use std::cell::{Cell, RefCell};

fn run(id: u64, status: &str, conclusion: &str, sha: &str) -> Run {
    Run {
        id,
        status: status.into(),
        conclusion: conclusion.into(),
        head_sha: sha.into(),
    }
}

/// A clock that moves only when the wait sleeps.
fn drive(
    batches: Vec<Result<Vec<Run>, String>>,
    deadline: u64,
    poll: u64,
    sha: Option<&str>,
) -> Outcome {
    let elapsed = Cell::new(0u64);
    let queue = RefCell::new(batches.into_iter());
    wait_ci(
        &mut || queue.borrow_mut().next().unwrap_or_else(|| Ok(vec![])),
        &mut |d| elapsed.set(elapsed.get() + d.as_secs()),
        &mut || Duration::from_secs(elapsed.get()),
        Duration::from_secs(deadline),
        Duration::from_secs(poll),
        sha,
    )
}

#[test]
fn a_completed_green_run_ends_the_wait_even_when_jobs_were_skipped() {
    // Only the run is read: jobs skipped inside it, or checks that never complete, cannot hold it.
    let out = drive(
        vec![
            Ok(vec![run(7, "in_progress", "", "h")]),
            Ok(vec![run(7, "completed", "success", "h")]),
        ],
        600,
        20,
        Some("h"),
    );
    assert_eq!(
        out,
        Outcome::Green {
            detail: "run=7 conclusion=success".into()
        }
    );
    assert_eq!(out.code(), 0);
}

#[test]
fn the_newest_run_of_the_head_decides_not_an_older_red_one() {
    let runs = vec![
        run(5, "completed", "failure", "h"),
        run(9, "completed", "success", "h"),
        run(10, "completed", "failure", "other"),
    ];
    assert_eq!(newest_run(&runs, Some("h")).unwrap().id, 9);
    let out = drive(vec![Ok(runs)], 60, 20, Some("h"));
    assert_eq!(out.code(), 0, "{out:?}");
}

#[test]
fn a_rerun_that_is_newer_than_a_green_run_is_waited_for() {
    let out = drive(
        vec![
            Ok(vec![
                run(5, "completed", "success", "h"),
                run(6, "queued", "", "h"),
            ]),
            Ok(vec![
                run(5, "completed", "success", "h"),
                run(6, "completed", "failure", "h"),
            ]),
        ],
        600,
        20,
        Some("h"),
    );
    assert_eq!(
        out,
        Outcome::Red {
            detail: "run=6 conclusion=failure".into()
        }
    );
    assert_eq!(out.code(), 1);
}

#[test]
fn the_deadline_names_the_pending_run() {
    let out = drive(
        (0..100)
            .map(|_| Ok(vec![run(7, "in_progress", "", "h")]))
            .collect(),
        100,
        20,
        Some("h"),
    );
    let Outcome::Timeout { detail } = &out else {
        panic!("{out:?}")
    };
    assert!(
        detail.contains("deadline 100s") && detail.contains("run=7 status=in_progress"),
        "{detail}"
    );
    assert_eq!(out.code(), 3);
}

#[test]
fn a_head_without_a_run_and_a_failing_gh_still_end_at_the_deadline() {
    let out = drive(
        vec![
            Ok(vec![run(3, "completed", "success", "old")]),
            Err("boom".into()),
            Err("boom".into()),
        ],
        60,
        20,
        Some("h"),
    );
    let Outcome::Timeout { detail } = &out else {
        panic!("{out:?}")
    };
    assert!(detail.contains("deadline 60s"), "{detail}");
}

#[test]
fn skipped_and_neutral_conclusions_are_green() {
    for conclusion in ["skipped", "neutral"] {
        assert_eq!(
            drive(
                vec![Ok(vec![run(1, "completed", conclusion, "h")])],
                60,
                20,
                None
            )
            .code(),
            0
        );
    }
    for conclusion in ["failure", "cancelled", "timed_out"] {
        assert_eq!(
            drive(
                vec![Ok(vec![run(1, "completed", conclusion, "h")])],
                60,
                20,
                None
            )
            .code(),
            1
        );
    }
}

#[test]
fn gh_run_rows_are_read_by_database_id() {
    let text = r#"[{"databaseId":45370719249,"status":"completed","conclusion":"success","headSha":"abc"},{"status":"queued"}]"#;
    assert_eq!(
        runs_from_json(text).unwrap(),
        vec![run(45370719249, "completed", "success", "abc")]
    );
}

#[test]
fn status_files_end_at_name_done_or_blocked() {
    assert_eq!(status_state("DONE plangraph:A abc\n"), FileState::Pending);
    assert_eq!(
        status_state("DONE plangraph:A abc\npr10 DONE\nHEAD: x\n"),
        FileState::Done
    );
    assert_eq!(status_state("BLOCKED review no leg\n"), FileState::Blocked);
}

fn files(statuses: &[PathBuf], results: &[PathBuf], deadline: u64) -> Outcome {
    let elapsed = Cell::new(0u64);
    wait_files(
        statuses,
        results,
        &mut |d| elapsed.set(elapsed.get() + d.as_secs()),
        &mut || Duration::from_secs(elapsed.get()),
        Duration::from_secs(deadline),
        Duration::from_secs(10),
    )
}

#[test]
fn files_wait_for_every_status_and_result_and_name_the_pending_one() {
    let dir = tempfile::tempdir().unwrap();
    let done = dir.path().join("a.status");
    let open = dir.path().join("b.status");
    let seat = dir.path().join("seat.result");
    std::fs::write(&done, "x DONE\n").unwrap();
    std::fs::write(&open, "DONE k s\n").unwrap();
    let Outcome::Timeout { detail } = files(
        &[done.clone(), open.clone()],
        std::slice::from_ref(&seat),
        30,
    ) else {
        panic!()
    };
    assert!(
        detail.contains("b.status")
            && detail.contains("seat.result")
            && !detail.contains("a.status"),
        "{detail}"
    );
    std::fs::write(&open, "b DONE\n").unwrap();
    std::fs::write(&seat, "{}").unwrap();
    assert_eq!(files(&[done.clone(), open], &[seat], 30).code(), 0);
    std::fs::write(&done, "BLOCKED review x\n").unwrap();
    assert_eq!(files(&[done], &[], 30).code(), 1);
}

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
