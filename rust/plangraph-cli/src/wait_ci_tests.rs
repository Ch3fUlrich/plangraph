use super::*;
use std::cell::{Cell, RefCell};

fn run(id: u64, status: &str, conclusion: &str, sha: &str) -> Run {
    Run {
        id,
        status: status.into(),
        conclusion: conclusion.into(),
        head_sha: sha.into(),
        updated_at: String::new(),
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
        &mut |_| queue.borrow_mut().next().unwrap_or_else(|| Ok(vec![])),
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
fn a_slow_gh_cannot_push_the_wait_past_its_deadline() {
    // Every fetch takes 50 s of the clock; the budget handed to it shrinks and the wait ends at 30 s.
    let elapsed = Cell::new(0u64);
    let budgets = RefCell::new(Vec::new());
    let out = wait_ci(
        &mut |budget| {
            budgets.borrow_mut().push(budget.as_secs());
            elapsed.set(elapsed.get() + budget.as_secs());
            Ok(vec![run(7, "queued", "", "h")])
        },
        &mut |d| elapsed.set(elapsed.get() + d.as_secs()),
        &mut || Duration::from_secs(elapsed.get()),
        Duration::from_secs(30),
        Duration::from_secs(20),
        Some("h"),
    );
    assert_eq!(*budgets.borrow(), vec![30], "{budgets:?}");
    assert_eq!(out.code(), 3);
    assert!(elapsed.get() <= 30, "elapsed {}", elapsed.get());
}

#[test]
fn a_poll_longer_than_the_remainder_sleeps_only_the_remainder() {
    let elapsed = Cell::new(0u64);
    let sleeps = RefCell::new(Vec::new());
    let out = wait_ci(
        &mut |_| Ok(vec![run(7, "queued", "", "h")]),
        &mut |d| {
            sleeps.borrow_mut().push(d.as_secs());
            elapsed.set(elapsed.get() + d.as_secs());
        },
        &mut || Duration::from_secs(elapsed.get()),
        Duration::from_secs(30),
        Duration::from_secs(20),
        Some("h"),
    );
    // 20 s then the 10 s that remain: the wait runs to its deadline, not 10 s short of it.
    assert_eq!(*sleeps.borrow(), vec![20, 10]);
    assert_eq!(elapsed.get(), 30);
    assert_eq!(out.code(), 3);
}
