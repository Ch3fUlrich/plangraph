use crate::step_runner::*;
use crate::step_runner_shell::CommandRunner;
use crate::step_services::*;
use crate::step_services_cli::*;
use pg_worker_core::batch_steps::*;
use std::{collections::VecDeque, path::Path, time::Duration};

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
                run(6, "completed", Some("skipped")),
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
    // The green run the train join consumes is the CI class output, not a made up id.
    assert_eq!(actions.train.0, vec![(HEAD.to_owned(), 12, 6, 1)]);
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
    assert!(matches!(classify(&[], &[]), Reading::Pending(_)));
}

#[test]
fn a_cancelled_run_is_red() {
    let Reading::Done(class) = classify(&[run(3, "completed", Some("cancelled"))], &[]) else {
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
            run(4, "in_progress", None),
        ],
        &[],
    ) else {
        panic!("pending");
    };
    assert!(matches!(class.classification, Classification::Red));
}

/// Runner that answers by the first words of the command and keeps every command.
struct Script {
    answers: VecDeque<Result<String, String>>,
    seen: Vec<Vec<String>>,
}

impl Script {
    fn new(answers: Vec<Result<&str, &str>>) -> Self {
        Self {
            answers: answers
                .into_iter()
                .map(|answer| answer.map(str::to_owned).map_err(str::to_owned))
                .collect(),
            seen: Vec::new(),
        }
    }
}

impl CommandRunner for &mut Script {
    fn run(&mut self, _: &Path, argv: &[String]) -> Result<String, String> {
        self.seen.push(argv.to_vec());
        self.answers.pop_front().unwrap_or(Err("no answer".into()))
    }
}

fn keys() -> Vec<String> {
    vec!["demo:A".into(), "demo:B".into()]
}

#[test]
fn keeper_cli_reads_the_epochs_of_a_claim_batch() {
    let mut script = Script::new(vec![Ok(
        r#"{"results":[{"key":"demo:A","epoch":3},{"key":"demo:B","epoch":9}]}"#,
    )]);
    let mut keeper = KeeperCli {
        runner: &mut script,
        dir: ".".into(),
        prefix: vec!["pg".into(), "--json".into()],
    };
    let held = keeper.claim_batch(&keys()).unwrap();
    assert_eq!(held, vec![("demo:A".into(), 3), ("demo:B".into(), 9)]);
    assert_eq!(
        script.seen[0],
        ["pg", "--json", "claim-batch", "demo:A", "demo:B"]
    );
}

#[test]
fn keeper_cli_releases_only_the_epochs_this_attempt_got() {
    // demo:A was granted at epoch 3, demo:B is held by someone else: only A goes back.
    let mut script = Script::new(vec![
        Ok(
            r#"{"results":[{"key":"demo:A","epoch":3},{"key":"demo:B","error":"claimed","msg":"held"}]}"#,
        ),
        Ok("{}"),
    ]);
    let mut keeper = KeeperCli {
        runner: &mut script,
        dir: ".".into(),
        prefix: vec!["pg".into()],
    };
    let error = keeper.claim_batch(&keys()).unwrap_err();
    assert!(error.contains("demo:B"), "{error}");
    assert_eq!(
        script.seen.len(),
        2,
        "no release for the key another batch holds"
    );
    assert_eq!(
        script.seen[1],
        ["env", "PG_EPOCH=3", "pg", "release", "demo:A"]
    );
}

#[test]
fn keeper_cli_reports_a_release_that_failed() {
    let mut script = Script::new(vec![
        Ok(r#"{"results":[{"key":"demo:A","epoch":3},{"key":"demo:B","error":"claimed"}]}"#),
        Err("pg exit 2: refused"),
    ]);
    let mut keeper = KeeperCli {
        runner: &mut script,
        dir: ".".into(),
        prefix: vec!["pg".into()],
    };
    let error = keeper.claim_batch(&keys()).unwrap_err();
    assert!(error.contains("release failed: demo:A@3"), "{error}");
}

#[test]
fn keeper_cli_says_when_the_outcome_of_the_claim_is_unknown() {
    let mut script = Script::new(vec![Err("pg: unreachable")]);
    let mut keeper = KeeperCli {
        runner: &mut script,
        dir: ".".into(),
        prefix: vec!["pg".into()],
    };
    let error = keeper.claim_batch(&keys()).unwrap_err();
    assert!(error.contains("outcome unknown"), "{error}");
}

#[test]
fn keeper_cli_refuses_a_row_without_an_epoch() {
    let mut script = Script::new(vec![Ok(r#"{"results":[{"key":"demo:A","error":"held"}]}"#)]);
    let mut keeper = KeeperCli {
        runner: &mut script,
        dir: ".".into(),
        prefix: vec!["pg".into()],
    };
    assert!(keeper.claim_batch(&keys()).unwrap_err().contains("refused"));
}

#[test]
fn gh_ci_reads_only_the_runs_of_this_pull_request() {
    let mut script = Script::new(vec![Ok(r#"{"workflow_runs":[
            {"id":5,"name":"CI","run_attempt":2,"status":"completed","conclusion":"success","pull_requests":[{"number":12}]},
            {"id":6,"name":"CI","run_attempt":1,"status":"queued","conclusion":null,"pull_requests":[{"number":12}]},
            {"id":7,"name":"CI","run_attempt":1,"status":"completed","conclusion":"failure","pull_requests":[{"number":99}]},
            {"id":8,"name":"CI","run_attempt":1,"status":"completed","conclusion":"success","pull_requests":[]}]}"#)]);
    let mut ci = GhCi {
        runner: &mut script,
        dir: ".".into(),
        repo: "demo/repo".into(),
    };
    let runs = ci.runs(HEAD, 12).unwrap();
    assert_eq!(
        runs.len(),
        2,
        "another pull request's run and an unbound run are left out"
    );
    assert_eq!(runs[0], run(5, "completed", Some("success")).tap_attempt(2));
    assert_eq!(runs[1].conclusion, None);
    assert_eq!(
        script.seen[0],
        [
            "gh",
            "api",
            &format!("repos/demo/repo/actions/runs?head_sha={HEAD}&per_page=100")
        ]
    );
}

#[test]
fn green_needs_every_required_workflow_to_have_run() {
    let only_lint = CiRun {
        name: "lint".into(),
        ..run(4, "completed", Some("success"))
    };
    let required = ["CI".to_owned()];
    assert!(matches!(
        classify(std::slice::from_ref(&only_lint), &required),
        Reading::Pending(_)
    ));
    let ci = run(5, "completed", Some("success"));
    let Reading::Done(class) = classify(&[only_lint, ci], &required) else {
        panic!("pending");
    };
    assert_eq!(class.run, 5, "the run named is the required workflow's");
}

trait TapAttempt {
    fn tap_attempt(self, attempt: u32) -> Self;
}

impl TapAttempt for CiRun {
    fn tap_attempt(mut self, attempt: u32) -> Self {
        self.attempt = attempt;
        self
    }
}

#[test]
fn train_command_passes_the_ci_run_it_consumes() {
    let mut script = Script::new(vec![Ok(r#"{"train":"t-1","manifest":"m-1"}"#)]);
    let mut desk = TrainCommand {
        runner: &mut script,
        dir: ".".into(),
        argv: vec!["train-desk".into(), "join".into()],
    };
    let joined = desk.join(HEAD, 12, 6, 2).unwrap();
    assert_eq!(
        (joined.train.as_str(), joined.manifest.as_str()),
        ("t-1", "m-1")
    );
    assert_eq!(
        script.seen[0],
        [
            "train-desk",
            "join",
            "--head",
            HEAD,
            "--pr",
            "12",
            "--run",
            "6",
            "--attempt",
            "2"
        ]
    );
}

#[test]
fn train_command_without_a_command_fails_with_a_reason() {
    let mut script = Script::new(vec![]);
    let mut desk = TrainCommand {
        runner: &mut script,
        dir: ".".into(),
        argv: vec![],
    };
    assert!(desk
        .join(HEAD, 1, 1, 1)
        .unwrap_err()
        .contains("no train join command"));
}
