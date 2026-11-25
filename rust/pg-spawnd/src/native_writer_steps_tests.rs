use crate::native_writer::{quota, Driver};
use crate::native_writer_steps::AgentSteps;
use crate::native_writer_steps_fake::*;
use pg_spawnd::step_runner::*;
use pg_worker_core::batch_steps::*;
use serde_json::Value;

/// The deterministic steps the runner needs next to the agent steps.
struct Stack(AgentSteps<Fake>);

impl StepActions for Stack {
    fn claim(&mut self, _: &[String]) -> Result<Claim, String> {
        Ok(Claim {
            claim: "claim".into(),
        })
    }
    fn worktree(&mut self, _: &str, _: &str) -> Result<Worktree, String> {
        Ok(Worktree {
            worktree: "wt".into(),
        })
    }
    fn write(&mut self, worktree: &str, brief: &str) -> Result<Write, String> {
        self.0.write(worktree, brief)
    }
    fn fast_checks(&mut self, head: &str) -> Result<FastChecks, String> {
        self.0.driver.fast_checks(head)?;
        Ok(FastChecks {
            stamp: format!("stamp-{head}"),
        })
    }
    fn review(&mut self, head: &str, stamp: &str) -> Result<Review, String> {
        self.0.review(head, stamp)
    }
    fn fix(&mut self, head: &str, session: &str, review: &str) -> Result<Fix, String> {
        self.0.fix(head, session, review)
    }
    fn push(&mut self, _: &str, _: &str, _: &str) -> Result<Push, String> {
        Ok(Push { pr: 3 })
    }
    fn ci_class(&mut self, _: &str, _: u64) -> Result<CiClass, String> {
        Ok(CiClass {
            classification: Classification::Red,
            run: 1,
            attempt: 1,
        })
    }
    fn train_join(&mut self, _: &str, _: u64, _: u64, _: u32) -> Result<TrainJoin, String> {
        unreachable!()
    }
}

/// Keeps the step names, and the list in the steps file like the subcommand does.
struct Keep(Vec<String>, pg_spawnd::step_runner_log::StepLog);

impl StageSink for Keep {
    fn record(&mut self, steps: &BatchSteps) -> Result<(), String> {
        self.0
            .push(step_name(&steps.rows.last().unwrap().step).to_owned());
        self.1.record(steps)
    }
}

#[test]
fn the_runner_drives_write_review_fix_review_over_the_driver() {
    let dir = tempfile::tempdir().unwrap();
    let fake = Fake {
        verdicts: vec!["REQUEST_CHANGES", "ACCEPT"],
        ..Fake::default()
    };
    let mut stack = Stack(agents(fake, dir.path()));
    let inputs = Inputs {
        repo: "demo/repo".into(),
        batch_key: "b".into(),
        items: vec!["demo:A".into()],
        base: BASE.into(),
        brief: brief(dir.path()),
    };
    let mut runner = Runner::new(inputs, Limits::default());
    let mut sink = Keep(
        Vec::new(),
        pg_spawnd::step_runner_log::StepLog {
            path: dir.path().join("steps.json"),
            journal_repo: None,
            item_keys: vec![],
            base: BASE.into(),
        },
    );
    let mut now = 0;
    let outcome = runner.run(&mut stack, &mut sink, &mut || {
        now += 1;
        now
    });
    // The list ends at the red CI the stack reports; the agent steps ran in order before it.
    assert!(matches!(outcome, Outcome::CiRed { .. }), "{outcome:?}");
    assert_eq!(
        sink.0,
        [
            "claim",
            "worktree",
            "write",
            "fast_checks",
            "review",
            "fix",
            "fast_checks",
            "review",
            "push",
            "ci_class"
        ]
    );
    let fake = &stack.0.driver;
    assert_eq!(fake.turns[0].0, None, "write starts a session");
    assert_eq!(
        fake.turns[1].0.as_deref(),
        Some("writer-session"),
        "fix resumes it"
    );
    assert!(
        fake.turns[1].1.contains("HIGH a.rs:1 - finding"),
        "{}",
        fake.turns[1].1
    );
    assert_eq!(fake.reviews[0].0, sha('1'));
    assert_eq!(
        fake.reviews[1].0,
        sha('2'),
        "the second review sees the fixed head"
    );
    assert!(
        fake.reviews[0].1.is_none() && fake.reviews[1].1.is_some(),
        "delta review"
    );
}

#[test]
fn a_writer_without_a_commit_fails_the_write() {
    let dir = tempfile::tempdir().unwrap();
    let mut steps = agents(Fake::default(), dir.path());
    steps.base = sha('1');
    let error = steps.write("wt", &brief(dir.path())).unwrap_err();
    assert!(error.contains("no commit beyond the base"), "{error}");
}

#[test]
fn a_review_without_the_stamp_is_refused() {
    let dir = tempfile::tempdir().unwrap();
    let mut steps = agents(Fake::default(), dir.path());
    let error = steps.review(&sha('1'), "stamp").unwrap_err();
    assert!(error.contains("no fast-checks stamp"), "{error}");
}

#[test]
fn a_review_on_another_head_is_no_verdict() {
    let dir = tempfile::tempdir().unwrap();
    struct Moved(Fake);
    impl Driver for Moved {
        fn prepare(&mut self) -> Result<String, String> {
            self.0.prepare()
        }
        fn refresh_base(&mut self) -> Result<String, String> {
            self.0.refresh_base()
        }
        fn disk_gb(&mut self) -> Result<u64, String> {
            self.0.disk_gb()
        }
        fn execute(&mut self, s: Option<&str>, p: &str) -> Result<String, String> {
            self.0.execute(s, p)
        }
        fn head(&mut self) -> Result<String, String> {
            self.0.head()
        }
        fn has_commits_beyond(&mut self, b: &str, h: &str) -> Result<bool, String> {
            self.0.has_commits_beyond(b, h)
        }
        fn fast_checks(&mut self, h: &str) -> Result<(), String> {
            self.0.fast_checks(h)
        }
        fn stamped(&mut self, _: &str) -> bool {
            true
        }
        fn review(&mut self, _: &str, p: Option<&Value>) -> Result<Value, String> {
            self.0.review(&sha('9'), p)
        }
        fn gates(&mut self) -> Result<(), String> {
            Ok(())
        }
        fn save(&mut self, _: &Value) -> Result<(), String> {
            Ok(())
        }
    }
    let mut steps = AgentSteps::new(
        Moved(Fake::default()),
        BASE.into(),
        dir.path().join("f"),
        dir.path().join("steps.json"),
    );
    let error = steps.review(&sha('1'), "stamp").unwrap_err();
    assert!(error.contains("another head"), "{error}");
}

#[test]
fn a_fix_that_changes_nothing_fails() {
    let dir = tempfile::tempdir().unwrap();
    let mut steps = agents(
        Fake {
            no_commit: true,
            verdicts: vec!["REQUEST_CHANGES"],
            heads: vec![sha('1')],
            stamped: vec![sha('1')],
            ..Fake::default()
        },
        dir.path(),
    );
    let review = steps.review(&sha('1'), "stamp").unwrap();
    commit(&steps.steps, &sha('1'), &review);
    let error = steps
        .fix(&sha('1'), "writer-session", &review.review)
        .unwrap_err();
    assert!(error.contains("unchanged"), "{error}");
    assert_eq!(
        steps.driver.turns.len(),
        1,
        "the turn ran; only its result was refused"
    );
}

#[test]
fn a_fix_of_a_review_that_was_never_kept_fails_without_a_turn() {
    let dir = tempfile::tempdir().unwrap();
    let mut steps = agents(Fake::default(), dir.path());
    let error = steps.fix(&sha('1'), "s", "run-missing").unwrap_err();
    assert!(error.contains("not a recorded review"), "{error}");
    assert!(steps.driver.turns.is_empty());
}

#[test]
fn a_usage_limit_is_booked_and_stops_every_later_turn() {
    let dir = tempfile::tempdir().unwrap();
    let limit =
        quota::classify("You've hit your usage limit. Or try again at Nov 27th, 2026 8:24 PM.")
            .unwrap()
            .error();
    let fake = Fake {
        execute_error: Some(limit.clone()),
        ..Fake::default()
    };
    let mut steps = agents(fake, dir.path());
    let brief = brief(dir.path());
    assert_eq!(steps.write("wt", &brief).unwrap_err(), limit);
    assert_eq!(steps.write("wt", &brief).unwrap_err(), limit);
    assert_eq!(
        steps.driver.turns.len(),
        1,
        "the second write spent no turn"
    );
    assert_eq!(steps.driver.booked, 1);
}

#[test]
fn the_deterministic_steps_are_not_agent_steps() {
    let dir = tempfile::tempdir().unwrap();
    let mut steps = agents(Fake::default(), dir.path());
    assert!(steps
        .push("h", "s", "r")
        .unwrap_err()
        .contains("not an agent step"));
    assert!(steps.claim(&[]).is_err());
}

#[test]
fn the_worktree_step_prepares_the_native_base_and_checks_it() {
    let dir = tempfile::tempdir().unwrap();
    let mut steps = agents(Fake::default(), dir.path());
    let made = steps.worktree("claim", BASE).unwrap();
    assert_eq!(made.worktree, "/nonexistent/worktree");
    assert_eq!(
        steps.driver.prepared, 1,
        "the driver pinned the base the review reads"
    );
    let mut moved = agents(
        Fake {
            moved_base: Some(sha('7')),
            ..Fake::default()
        },
        dir.path(),
    );
    assert!(moved.worktree("claim", BASE).unwrap_err().contains("not"));
}

#[test]
fn the_fast_check_stamp_is_the_one_the_driver_wrote_and_the_push_reads() {
    let dir = tempfile::tempdir().unwrap();
    let fake = Fake {
        stamp_dir: Some(dir.path().to_owned()),
        ..Fake::default()
    };
    let mut steps = agents(fake, dir.path());
    let head = sha('1');
    let checked = steps.fast_checks(&head).unwrap();
    assert_eq!(checked.stamp, dir.path().join(&head).to_string_lossy());
    steps.verify_stamp(&head, &checked.stamp).unwrap();
    assert!(
        steps.verify_stamp(&head, "/elsewhere/stamp").is_err(),
        "another path is no receipt"
    );
    assert!(
        steps.verify_stamp(&sha('2'), &checked.stamp).is_err(),
        "another head is no receipt"
    );
}

#[test]
fn a_driver_that_leaves_no_stamp_fails_the_fast_checks() {
    let dir = tempfile::tempdir().unwrap();
    let mut steps = agents(Fake::default(), dir.path());
    let error = steps.fast_checks(&sha('1')).unwrap_err();
    assert!(error.contains("left no stamp"), "{error}");
}
