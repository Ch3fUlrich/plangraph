use crate::step_runner::*;
use crate::step_services::*;
use pg_worker_core::batch_steps::*;
use std::{collections::VecDeque, time::Duration};

const BASE: &str = "0000000000000000000000000000000000000000";
const HEAD: &str = "1111111111111111111111111111111111111111";

fn inputs() -> Inputs {
    Inputs {
        repo: "demo/repo".into(),
        batch_key: "batch-1".into(),
        items: vec!["demo:A".into(), "demo:B".into()],
        base: BASE.into(),
        brief: "brief-1".into(),
    }
}

fn run(id: u64, status: &str, conclusion: Option<&str>) -> CiRun {
    CiRun {
        name: "CI".into(),
        id,
        attempt: 1,
        status: status.into(),
        conclusion: conclusion.map(str::to_owned),
    }
}

/// Keeper that claims every key at epoch 4 and remembers what it was asked.
#[derive(Default)]
struct FakeKeeper(Vec<Vec<String>>);

impl Keeper for FakeKeeper {
    fn claim_batch(&mut self, keys: &[String]) -> Result<Vec<(String, u64)>, String> {
        self.0.push(keys.to_vec());
        Ok(keys.iter().map(|key| (key.clone(), 4)).collect())
    }
}

/// CI that shows one list of runs per look and repeats the last.
struct FakeCi(VecDeque<Vec<CiRun>>, usize);

impl CiRuns for FakeCi {
    fn runs(&mut self, _: &str, _: u64) -> Result<Vec<CiRun>, String> {
        self.1 += 1;
        if self.0.len() > 1 {
            return Ok(self.0.pop_front().unwrap());
        }
        Ok(self.0.front().cloned().unwrap_or_default())
    }
}

#[derive(Default)]
struct FakeTrain(Vec<(String, u64, u64, u32)>);

impl TrainDesk for FakeTrain {
    fn join(&mut self, head: &str, pr: u64, run: u64, attempt: u32) -> Result<TrainJoin, String> {
        self.0.push((head.into(), pr, run, attempt));
        Ok(TrainJoin {
            train: "train-7".into(),
            manifest: "manifest-7".into(),
        })
    }
}

/// The six steps the service steps pass on, all succeeding.
struct Rest;

impl StepActions for Rest {
    fn claim(&mut self, _: &[String]) -> Result<Claim, String> {
        unreachable!("claim is a service step")
    }
    fn worktree(&mut self, _: &str, _: &str) -> Result<Worktree, String> {
        Ok(Worktree {
            worktree: "wt".into(),
        })
    }
    fn write(&mut self, _: &str, _: &str) -> Result<Write, String> {
        Ok(Write {
            head: HEAD.into(),
            session: "s".into(),
        })
    }
    fn fast_checks(&mut self, _: &str) -> Result<FastChecks, String> {
        Ok(FastChecks {
            stamp: "stamp".into(),
        })
    }
    fn review(&mut self, _: &str, _: &str) -> Result<Review, String> {
        Ok(Review {
            verdict: Verdict::Accept,
            review: "run-1".into(),
        })
    }
    fn fix(&mut self, _: &str, _: &str, _: &str) -> Result<Fix, String> {
        unreachable!("the review accepts")
    }
    fn push(&mut self, _: &str, _: &str, _: &str) -> Result<Push, String> {
        Ok(Push { pr: 12 })
    }
    fn ci_class(&mut self, _: &str, _: u64) -> Result<CiClass, String> {
        unreachable!("ci_class is a service step")
    }
    fn train_join(&mut self, _: &str, _: u64, _: u64, _: u32) -> Result<TrainJoin, String> {
        unreachable!("train_join is a service step")
    }
}

struct Keep(usize);

impl StageSink for Keep {
    fn record(&mut self, steps: &BatchSteps) -> Result<(), String> {
        self.0 = steps.rows.len();
        Ok(())
    }
}

fn services(ci: Vec<Vec<CiRun>>, polls: u32) -> ServiceSteps<FakeKeeper, FakeCi, FakeTrain, Rest> {
    ServiceSteps {
        keeper: FakeKeeper::default(),
        ci: FakeCi(ci.into(), 0),
        train: FakeTrain::default(),
        wait: CiWait {
            polls,
            interval: Duration::ZERO,
        },
        required: vec!["CI".into()],
        draft_part: false,
        sleep: Box::new(|_| {}),
        rest: Rest,
    }
}

fn drive(actions: &mut dyn StepActions) -> (Outcome, usize) {
    let mut runner = Runner::new(inputs(), Limits::default());
    let mut sink = Keep(0);
    let mut clock = {
        let mut now = 0;
        move || {
            now += 1;
            now
        }
    };
    let outcome = runner.run(actions, &mut sink, &mut clock);
    (outcome, sink.0)
}

#[test]
fn a_batch_runs_to_train_join_through_the_real_actions() {
    let mut actions = services(
        vec![
            vec![],
            vec![run(5, "in_progress", None)],
            vec![
                run(5, "completed", Some("success")),
                CiRun {
                    name: "lint".into(),
                    ..run(6, "completed", Some("skipped"))
                },
            ],
        ],
        5,
    );
    let (outcome, rows) = drive(&mut actions);
    assert_eq!(
        outcome,
        Outcome::Joined {
            train: "train-7".into(),
            manifest: "manifest-7".into()
        }
    );
    assert_eq!(
        rows, 8,
        "claim worktree write fast review push ci train: one row each"
    );
    assert_eq!(
        actions.keeper.0,
        vec![vec!["demo:A".to_owned(), "demo:B".to_owned()]]
    );
    // The run the train join consumes is the required workflow's passing run.
    assert_eq!(actions.train.0, vec![(HEAD.to_owned(), 12, 5, 1)]);
}

#[test]
fn the_claim_reference_names_every_key_and_epoch() {
    let mut actions = services(vec![], 1);
    let claim = actions.claim(&inputs().items).unwrap();
    assert_eq!(claim.claim, "demo:A@4,demo:B@4");
}

#[test]
fn a_keeper_that_claims_other_keys_fails_the_claim() {
    struct Wrong;
    impl Keeper for Wrong {
        fn claim_batch(&mut self, _: &[String]) -> Result<Vec<(String, u64)>, String> {
            Ok(vec![("demo:A".into(), 1)])
        }
    }
    let mut actions = services(vec![], 1);
    let mut wrong = ServiceSteps {
        keeper: Wrong,
        ci: FakeCi(VecDeque::new(), 0),
        train: FakeTrain::default(),
        wait: actions.wait,
        required: vec![],
        draft_part: false,
        sleep: Box::new(|_| {}),
        rest: Rest,
    };
    assert!(wrong.claim(&inputs().items).unwrap_err().contains("not"));
    let _ = &mut actions;
}

#[test]
fn a_red_run_ends_the_list_with_the_failing_run() {
    let mut actions = services(
        vec![vec![
            run(5, "completed", Some("success")),
            run(8, "completed", Some("failure")),
        ]],
        3,
    );
    let (outcome, _) = drive(&mut actions);
    assert_eq!(
        outcome,
        Outcome::CiRed {
            head: HEAD.into(),
            pr: 12
        }
    );
    assert!(actions.train.0.is_empty(), "a red head never joins a train");
}

#[test]
fn a_run_that_never_finishes_fails_ci_class_after_the_looks() {
    let mut actions = services(vec![vec![run(5, "in_progress", None)]], 3);
    let error = actions.ci_class(HEAD, 12).unwrap_err();
    assert!(error.contains("after 3 looks"), "{error}");
    assert_eq!(actions.ci.1, 3);
}

#[test]
fn a_head_without_runs_is_never_green() {
    assert!(matches!(classify(&[], &[], false), Reading::Pending(_)));
}

#[test]
fn a_cancelled_run_is_red() {
    let Reading::Done(class) = classify(&[run(3, "completed", Some("cancelled"))], &[], false)
    else {
        panic!("pending");
    };
    assert!(matches!(class.classification, Classification::Red));
    assert_eq!(class.run, 3);
}

#[test]
fn red_wins_over_a_run_still_going() {
    let Reading::Done(class) = classify(
        &[
            run(3, "completed", Some("failure")),
            CiRun {
                name: "lint".into(),
                ..run(4, "in_progress", None)
            },
        ],
        &[],
        false,
    ) else {
        panic!("pending");
    };
    assert!(matches!(class.classification, Classification::Red));
}

#[test]
fn green_needs_every_required_workflow_to_have_run() {
    let only_lint = CiRun {
        name: "lint".into(),
        ..run(4, "completed", Some("success"))
    };
    let required = ["CI".to_owned()];
    assert!(matches!(
        classify(std::slice::from_ref(&only_lint), &required, false),
        Reading::Pending(_)
    ));
    let ci = run(5, "completed", Some("success"));
    let Reading::Done(class) = classify(&[only_lint, ci], &required, false) else {
        panic!("pending");
    };
    assert_eq!(class.run, 5, "the run named is the required workflow's");
}

#[test]
fn a_required_workflow_that_only_skipped_is_not_green() {
    let mut actions = services(vec![vec![run(5, "completed", Some("skipped"))]], 5);
    let error = actions.ci_class(HEAD, 12).unwrap_err();
    assert!(
        error.contains("required workflow CI has no passing run"),
        "{error}"
    );
    assert_eq!(
        actions.ci.1, 1,
        "waiting longer cannot change a skipped run"
    );
}

#[test]
fn an_older_failed_run_of_a_workflow_does_not_make_the_head_red() {
    let old_red = run(3, "completed", Some("failure"));
    let new_green = run(7, "completed", Some("success"));
    let Reading::Done(class) = classify(&[old_red, new_green], &["CI".to_owned()], false) else {
        panic!("pending");
    };
    assert!(matches!(class.classification, Classification::Green));
    assert_eq!(
        (class.run, class.attempt),
        (7, 1),
        "the newer run is the receipt"
    );
}

#[test]
fn a_newer_failure_beats_an_older_pass() {
    let Reading::Done(class) = classify(
        &[
            run(3, "completed", Some("success")),
            run(7, "completed", Some("failure")),
        ],
        &["CI".to_owned()],
        false,
    ) else {
        panic!("pending");
    };
    assert!(matches!(class.classification, Classification::Red));
    assert_eq!(class.run, 7);
}

#[test]
fn a_draft_lane_part_passes_on_its_skipped_required_workflow() {
    // The policy of .github/workflows/tests.yml: a draft lane/* pull request runs no job and a
    // skipped required check reports success; its admission is review and the fast gates.
    let skipped = [run(5, "completed", Some("skipped"))];
    let required = ["CI".to_owned()];
    assert!(matches!(
        classify(&skipped, &required, false),
        Reading::Unproven(_)
    ));
    let Reading::Done(class) = classify(&skipped, &required, true) else {
        panic!("not decided");
    };
    assert!(matches!(class.classification, Classification::Green));
    assert_eq!(class.run, 5);
    // The policy never excuses a failure.
    let Reading::Done(red) = classify(&[run(6, "completed", Some("failure"))], &required, true)
    else {
        panic!("pending");
    };
    assert!(matches!(red.classification, Classification::Red));
}
