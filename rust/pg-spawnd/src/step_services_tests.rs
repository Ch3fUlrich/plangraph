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
        event: "pull_request".into(),
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
struct FakeCi(VecDeque<Vec<CiRun>>, usize, bool);

impl CiRuns for FakeCi {
    fn draft_lane_part(&mut self, _: u64) -> Result<bool, String> {
        Ok(self.2)
    }
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
        ci: FakeCi(ci.into(), 0, false),
        train: FakeTrain::default(),
        wait: CiWait {
            polls,
            interval: Duration::ZERO,
        },
        required: vec!["CI".into()],
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
        ci: FakeCi(VecDeque::new(), 0, false),
        train: FakeTrain::default(),
        wait: actions.wait,
        required: vec![],
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
fn ci_class_asks_the_pull_request_whether_it_is_a_draft_part() {
    let skipped = || vec![vec![run(5, "completed", Some("skipped"))]];
    // The pull request is not a draft lane part (a reused PR, marked ready): no exception.
    let mut ready = services(skipped(), 3);
    let error = ready.ci_class(HEAD, 12).unwrap_err();
    assert!(error.contains("no passing run"), "{error}");
    let mut draft = services(skipped(), 3);
    draft.ci.2 = true;
    let class = draft.ci_class(HEAD, 12).unwrap();
    assert!(matches!(class.classification, Classification::Green));
    assert_eq!(class.run, 5);
}
