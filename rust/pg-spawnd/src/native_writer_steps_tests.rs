use crate::native_writer::{quota, Driver};
use crate::native_writer_steps::AgentSteps;
use pg_spawnd::step_runner::*;

use pg_worker_core::batch_steps::*;
use serde_json::{json, Value};

const BASE: &str = "0000000000000000000000000000000000000000";

fn sha(digit: char) -> String {
    digit.to_string().repeat(40)
}

/// A driver whose writer commits a new head per turn and whose reviews follow a script.
#[derive(Default)]
struct Fake {
    /// `(session, prompt)` of every writer turn.
    turns: Vec<(Option<String>, String)>,
    heads: Vec<String>,
    verdicts: Vec<&'static str>,
    reviews: Vec<(String, Option<Value>)>,
    stamped: Vec<String>,
    booked: usize,
    execute_error: Option<String>,
    /// Writer turns that leave HEAD where it was.
    no_commit: bool,
    /// Where stamps are written; fast checks of a fake without one leave none.
    stamp_dir: Option<std::path::PathBuf>,
    prepared: usize,
    /// HIGH finding lines per review text.
    findings: usize,
    /// The base `prepare` resolves, when not BASE.
    moved_base: Option<String>,
}

impl Driver for Fake {
    fn prepare(&mut self) -> Result<String, String> {
        self.prepared += 1;
        Ok(self.moved_base.clone().unwrap_or_else(|| BASE.into()))
    }
    fn worktree(&self) -> Option<std::path::PathBuf> {
        Some("/nonexistent/worktree".into())
    }
    fn stamp_path(&mut self, head: &str) -> Option<std::path::PathBuf> {
        self.stamp_dir.as_ref().map(|dir| dir.join(head))
    }
    fn refresh_base(&mut self) -> Result<String, String> {
        unreachable!()
    }
    fn disk_gb(&mut self) -> Result<u64, String> {
        Ok(100)
    }
    fn execute(&mut self, session: Option<&str>, prompt: &str) -> Result<String, String> {
        self.turns.push((session.map(str::to_owned), prompt.into()));
        if let Some(error) = &self.execute_error {
            return Err(error.clone());
        }
        if !self.no_commit {
            self.heads
                .push(sha(char::from(b'0' + self.turns.len() as u8)));
        }
        Ok("writer-session".into())
    }
    fn head(&mut self) -> Result<String, String> {
        Ok(self.heads.last().cloned().unwrap_or_else(|| BASE.into()))
    }
    fn has_commits_beyond(&mut self, base: &str, head: &str) -> Result<bool, String> {
        Ok(base != head)
    }
    fn fast_checks(&mut self, head: &str) -> Result<(), String> {
        self.stamped.push(head.into());
        if let Some(dir) = &self.stamp_dir {
            std::fs::write(dir.join(head), "green\n").map_err(|err| err.to_string())?;
        }
        Ok(())
    }
    fn stamped(&mut self, head: &str) -> bool {
        self.stamped.iter().any(|stamped| stamped == head)
    }
    fn review(&mut self, head: &str, previous: Option<&Value>) -> Result<Value, String> {
        self.reviews.push((head.into(), previous.cloned()));
        let verdict = if self.verdicts.is_empty() {
            "ACCEPT"
        } else {
            self.verdicts.remove(0)
        };
        Ok(
            json!({"verdict":verdict, "head":head, "run_id":format!("run-r{}", self.reviews.len()),
            "text":format!("{}VERDICT: {verdict}\n", "HIGH a.rs:1 - finding\n".repeat(self.findings.max(1)))}),
        )
    }
    fn gates(&mut self) -> Result<(), String> {
        Ok(())
    }
    fn record_quota(&mut self, _: &quota::Limit) -> Result<(), String> {
        self.booked += 1;
        Ok(())
    }
    fn save(&mut self, _: &Value) -> Result<(), String> {
        Ok(())
    }
}

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

struct Keep(Vec<String>);

impl StageSink for Keep {
    fn record(&mut self, steps: &BatchSteps) -> Result<(), String> {
        self.0
            .push(step_name(&steps.rows.last().unwrap().step).to_owned());
        Ok(())
    }
}

fn agents(fake: Fake, dir: &std::path::Path) -> AgentSteps<Fake> {
    AgentSteps::new(fake, BASE.into(), dir.join("findings"))
}

fn brief(dir: &std::path::Path) -> String {
    let path = dir.join("brief.md");
    std::fs::write(&path, "# batch\nitem text\n").unwrap();
    path.to_string_lossy().into_owned()
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
    let mut sink = Keep(Vec::new());
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
    let mut steps = AgentSteps::new(Moved(Fake::default()), BASE.into(), dir.path().join("f"));
    let error = steps.review(&sha('1'), "stamp").unwrap_err();
    assert!(error.contains("another head"), "{error}");
}

#[test]
fn a_fix_that_changes_nothing_fails() {
    let dir = tempfile::tempdir().unwrap();
    let mut steps = agents(
        Fake {
            no_commit: true,
            heads: vec![sha('1')],
            stamped: vec![sha('1')],
            ..Fake::default()
        },
        dir.path(),
    );
    let review = steps.review(&sha('1'), "stamp").unwrap();
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
    assert!(error.contains("unreadable"), "{error}");
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

/// A review round that asks for changes with `count` HIGH findings.
fn changes(steps: &mut AgentSteps<Fake>, head: &str, count: usize) -> Review {
    steps.driver.stamped.push(head.into());
    steps.driver.verdicts = vec!["REQUEST_CHANGES"];
    steps.driver.findings = count;
    steps.review(head, "stamp").unwrap()
}

#[test]
fn the_previous_review_survives_a_restart_for_the_delta_review() {
    let dir = tempfile::tempdir().unwrap();
    let head = sha('1');
    let mut first = agents(Fake::default(), dir.path());
    changes(&mut first, &head, 1);
    // A new process: the adapter and the driver are new, the findings directory is the same.
    let mut second = agents(Fake::default(), dir.path());
    second.driver.stamped.push(sha('2'));
    second.review(&sha('2'), "stamp").unwrap();
    let previous = second.driver.reviews[0].1.as_ref().expect("a delta review");
    assert_eq!(previous["head"], head.as_str());
    assert!(previous["text"].as_str().unwrap().contains("HIGH a.rs:1"));
    // An accepting review clears it: the next head is not a delta of a closed round.
    second.driver.stamped.push(sha('3'));
    second.review(&sha('3'), "stamp").unwrap();
    assert!(second.driver.reviews[1].1.is_none());
}

#[test]
fn rounds_that_do_not_improve_stop_the_batch_before_the_fix_turn() {
    let dir = tempfile::tempdir().unwrap();
    let mut steps = agents(Fake::default(), dir.path());
    let mut last = None;
    for round in 1..=3 {
        let head = sha(char::from(b'a' + round));
        let review = changes(&mut steps, &head, 2);
        last = Some((head, review.review));
        if round < 3 {
            let (head, review) = last.as_ref().unwrap();
            steps.fix(head, "writer-session", review).unwrap();
        }
    }
    let (head, review) = last.unwrap();
    let turns = steps.driver.turns.len();
    let error = steps.fix(&head, "writer-session", &review).unwrap_err();
    assert_eq!(error, "round_stop: no_progress");
    assert_eq!(steps.driver.turns.len(), turns, "the stop spent no turn");
}

#[test]
fn the_eighth_round_is_the_last_even_when_every_round_improves() {
    let dir = tempfile::tempdir().unwrap();
    let mut steps = agents(Fake::default(), dir.path());
    // Counts 128,64,..,1 halve every round, so only the cap ends it, at round 8.
    let mut review = None;
    for round in 1..=8u8 {
        let head = sha(char::from(b'a' + round));
        review = Some((
            head.clone(),
            changes(&mut steps, &head, 1usize << (8 - round)).review,
        ));
    }
    let (head, id) = review.unwrap();
    assert_eq!(
        steps.fix(&head, "s", &id).unwrap_err(),
        "round_stop: round_cap"
    );
}
