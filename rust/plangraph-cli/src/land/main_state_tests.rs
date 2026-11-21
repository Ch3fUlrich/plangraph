use super::main_state::*;
use std::collections::BTreeMap;

const T: i64 = 1800;

fn matrix() -> Matrix {
    let mut tiers = BTreeMap::new();
    let mut hosted = BTreeMap::new();
    for (job, suite) in [
        ("rust", "rust"),
        ("py 1 of 3", "sqlite"),
        ("py 2 of 3", "sqlite"),
        ("py 3 of 3", "sqlite"),
    ] {
        hosted.insert(job.to_owned(), suite.to_owned());
    }
    tiers.insert("hosted".to_owned(), hosted);
    let mut own = BTreeMap::new();
    for (job, suite) in [
        ("rust", "rust"),
        ("py 1 of 4", "sqlite"),
        ("py 2 of 4", "sqlite"),
        ("py 3 of 4", "sqlite"),
        ("py 4 of 4", "sqlite"),
    ] {
        own.insert(job.to_owned(), suite.to_owned());
    }
    tiers.insert("self".to_owned(), own);
    Matrix { tiers }
}

fn job(name: &str, conclusion: &str) -> Job {
    Job {
        name: name.into(),
        conclusion: Some(conclusion.into()),
        partial_log: false,
        tests: vec![],
    }
}

fn green_jobs(tier: &str) -> Vec<Job> {
    matrix().tiers[tier]
        .keys()
        .map(|n| job(n, "success"))
        .collect()
}

fn attempt(
    number: u32,
    status: &str,
    conclusion: Option<&str>,
    started: i64,
    tier: &str,
    jobs: Vec<Job>,
) -> Attempt {
    Attempt {
        number,
        status: status.into(),
        conclusion: conclusion.map(Into::into),
        started,
        tier: tier.into(),
        jobs,
    }
}

fn run(id: u64, event: &str, attempts: Vec<Attempt>) -> Run {
    Run {
        id,
        event: event.into(),
        attempts,
    }
}

fn green_run(id: u64, event: &str) -> Run {
    run(
        id,
        event,
        vec![attempt(
            1,
            "completed",
            Some("success"),
            0,
            "hosted",
            green_jobs("hosted"),
        )],
    )
}

fn state(runs: &[Run], now: i64) -> State {
    evaluate(&matrix(), runs, 0, now, T)
}

#[test]
fn a_green_push_run_is_green() {
    assert_eq!(state(&[green_run(1, "push")], 100), State::Green);
}

#[test]
fn a_failed_push_run_stays_red_after_a_green_scheduled_run_on_the_same_tip() {
    let mut jobs = green_jobs("hosted");
    jobs[0] = job("rust", "failure");
    let push = run(
        1,
        "push",
        vec![attempt(1, "completed", Some("failure"), 0, "hosted", jobs)],
    );
    let s = state(&[push, green_run(2, "schedule")], 100);
    assert!(
        matches!(s, State::Red { ref blocking } if blocking.len() == 1 && blocking[0].run == 1)
    );
    assert_eq!(s.refusal(), Some("main_red"));
}

#[test]
fn a_failed_scheduled_run_is_red_until_its_rerun_is_green() {
    let mut jobs = green_jobs("hosted");
    jobs[0] = job("rust", "failure");
    let failed = attempt(1, "completed", Some("failure"), 0, "hosted", jobs);
    let rerunning = run(
        2,
        "schedule",
        vec![
            failed.clone(),
            attempt(2, "in_progress", None, 50, "hosted", vec![]),
        ],
    );
    assert!(matches!(
        state(&[green_run(1, "push"), rerunning], 100),
        State::Red { .. }
    ));
    let cleared = run(
        2,
        "schedule",
        vec![
            failed,
            attempt(
                2,
                "completed",
                Some("success"),
                50,
                "hosted",
                green_jobs("hosted"),
            ),
        ],
    );
    assert_eq!(state(&[green_run(1, "push"), cleared], 200), State::Green);
}

#[test]
fn reruns_that_stall_before_the_suite_runs_keep_the_failure_in_the_set() {
    let mut jobs = green_jobs("hosted");
    jobs[0] = job("rust", "failure");
    let r = run(
        1,
        "push",
        vec![
            attempt(1, "completed", Some("failure"), 0, "hosted", jobs),
            attempt(
                2,
                "in_progress",
                None,
                100,
                "hosted",
                vec![job("py 1 of 3", "success")],
            ),
        ],
    );
    let set = failure_set(&matrix(), &r, 100 + T + 1, T);
    assert!(set
        .iter()
        .any(|f| matches!(f, Failure::Suite { suite, .. } if suite == "rust")));
    assert!(matches!(state(&[r], 100 + T + 1), State::Red { .. }));
}

#[test]
fn a_green_rerun_on_three_shards_discharges_a_failed_shard_of_four_by_its_suite() {
    let mut jobs = green_jobs("self");
    let index = jobs.iter().position(|j| j.name == "py 4 of 4").unwrap();
    jobs[index] = job("py 4 of 4", "failure");
    let failed = attempt(1, "completed", Some("failure"), 0, "self", jobs);
    let rerun_jobs: Vec<Job> = green_jobs("hosted");
    // The rerun lists only the sqlite suite (fallback tier names), not the rust job.
    let partial: Vec<Job> = rerun_jobs
        .iter()
        .filter(|j| j.name != "rust")
        .cloned()
        .collect();
    let r = run(
        1,
        "push",
        vec![
            failed,
            attempt(2, "completed", Some("success"), 10, "hosted", partial),
        ],
    );
    let set = failure_set(&matrix(), &r, 100, T);
    assert!(set
        .iter()
        .all(|f| !matches!(f, Failure::Suite { suite, .. } if suite == "sqlite")));
}

#[test]
fn a_tip_with_no_push_run_is_pending_then_red_none() {
    assert_eq!(evaluate(&matrix(), &[], 0, 14 * 60, T), State::Pending);
    match evaluate(&matrix(), &[], 0, 15 * 60, T) {
        State::Red { blocking } => assert_eq!(blocking[0].run, 0),
        other => panic!("{other:?}"),
    }
}

#[test]
fn a_queued_first_push_attempt_is_pending_until_it_stalls_at_the_bound() {
    let push = run(
        1,
        "push",
        vec![attempt(1, "queued", None, 0, "hosted", vec![])],
    );
    assert_eq!(state(std::slice::from_ref(&push), T - 1), State::Pending);
    match state(&[push], T) {
        State::Red { blocking } => assert!(blocking[0].stalled),
        other => panic!("{other:?}"),
    }
}

#[test]
fn a_hung_first_scheduled_attempt_blocks_at_the_bound() {
    let sched = run(
        2,
        "schedule",
        vec![attempt(1, "in_progress", None, 0, "hosted", vec![])],
    );
    assert_eq!(
        state(&[green_run(1, "push"), sched.clone()], T - 1),
        State::Green
    );
    assert!(matches!(
        state(&[green_run(1, "push"), sched], T),
        State::Red { .. }
    ));
}

#[test]
fn a_startup_failure_is_a_workflow_entry_cleared_only_by_a_complete_green_attempt() {
    let failed = attempt(1, "completed", Some("startup_failure"), 0, "hosted", vec![]);
    let r = run(1, "push", vec![failed.clone()]);
    assert_eq!(
        failure_set(&matrix(), &r, 10, T),
        vec![Failure::Workflow { attempt: 1 }]
    );
    let partial = attempt(
        2,
        "completed",
        Some("success"),
        5,
        "hosted",
        vec![job("rust", "success")],
    );
    let r2 = run(1, "push", vec![failed.clone(), partial]);
    assert_eq!(
        failure_set(&matrix(), &r2, 10, T),
        vec![
            Failure::Workflow { attempt: 1 },
            Failure::Workflow { attempt: 2 }
        ]
    );
    let full = attempt(
        2,
        "completed",
        Some("success"),
        5,
        "hosted",
        green_jobs("hosted"),
    );
    assert!(failure_set(&matrix(), &run(1, "push", vec![failed, full]), 10, T).is_empty());
}

#[test]
fn a_capped_log_adds_an_incomplete_entry_next_to_the_known_flake() {
    let mut bad = job("rust", "failure");
    bad.partial_log = true;
    bad.tests = vec!["flaky::test".into()];
    let mut jobs = green_jobs("hosted");
    jobs[0] = bad;
    let r = run(
        1,
        "push",
        vec![attempt(1, "completed", Some("failure"), 0, "hosted", jobs)],
    );
    let set = failure_set(&matrix(), &r, 10, T);
    assert!(set.contains(&Failure::Incomplete {
        suite: "rust".into()
    }));
    assert!(set.contains(&Failure::Suite {
        suite: "rust".into(),
        tests: vec!["flaky::test".into()]
    }));
}

#[test]
fn success_with_a_missing_expected_job_is_not_green() {
    let jobs: Vec<Job> = green_jobs("hosted")
        .into_iter()
        .filter(|j| j.name != "rust")
        .collect();
    let push = run(
        1,
        "push",
        vec![attempt(1, "completed", Some("success"), 0, "hosted", jobs)],
    );
    assert!(matches!(state(&[push], 10), State::Red { .. }));
}

#[test]
fn the_stall_bound_is_twice_the_median_of_the_last_twenty_but_at_least_thirty_minutes() {
    assert_eq!(stall_bound(&[]), 1800);
    assert_eq!(stall_bound(&[600; 5]), 1800);
    assert_eq!(stall_bound(&[1200, 1200, 1500]), 2400);
    let mut many = vec![10_000; 5];
    many.extend([1000; 20]);
    assert_eq!(stall_bound(&many), 2000);
}

#[test]
fn admission_refuses_everything_but_green_and_honours_the_exception_only_while_red() {
    let red = State::Red { blocking: vec![] };
    assert_eq!(admit(&State::Green, None), Ok(()));
    assert_eq!(admit(&red, None), Err("main_red"));
    assert_eq!(admit(&red, Some("EXCEPTION:main-red-fix")), Ok(()));
    assert_eq!(admit(&State::Green, Some("EXCEPTION:main-red-fix")), Ok(()));
    assert_eq!(
        admit(&State::Pending, Some("EXCEPTION:main-red-fix")),
        Err("main_pending")
    );
    let unknown = State::StateUnknown {
        failing_read: "x".into(),
    };
    assert_eq!(
        admit(&unknown, Some("EXCEPTION:main-red-fix")),
        Err("state_unknown")
    );
    assert_eq!(
        admit(&State::RefNotForwardOnly, Some("EXCEPTION:main-red-fix")),
        Err("ref_not_forward_only")
    );
    assert_eq!(
        admit(&red, Some("EXCEPTION:operator-order")),
        Err("main_red")
    );
}

struct Fake {
    forward: Result<bool, String>,
    tips: Vec<Result<String, String>>,
    runs: Vec<Result<Vec<Run>, String>>,
    clock: i64,
    tip_calls: usize,
    run_calls: usize,
}

impl Fake {
    fn new(tips: Vec<&str>, runs: Vec<Vec<Run>>) -> Fake {
        Fake {
            forward: Ok(true),
            tips: tips.into_iter().map(|t| Ok(t.to_owned())).collect(),
            runs: runs.into_iter().map(Ok).collect(),
            clock: 10,
            tip_calls: 0,
            run_calls: 0,
        }
    }
}

impl Source for Fake {
    fn forward_only(&mut self) -> Result<bool, String> {
        self.forward.clone()
    }
    fn tip(&mut self) -> Result<String, String> {
        let i = self.tip_calls.min(self.tips.len() - 1);
        self.tip_calls += 1;
        self.tips[i].clone()
    }
    fn runs(&mut self, _: &str) -> Result<Vec<Run>, String> {
        let i = self.run_calls.min(self.runs.len() - 1);
        self.run_calls += 1;
        self.runs[i].clone()
    }
    fn green_push_durations(&mut self) -> Result<Vec<i64>, String> {
        Ok(vec![])
    }
    fn now(&mut self) -> i64 {
        self.clock
    }
}

#[derive(Default)]
struct Ledger {
    tips: Vec<String>,
    stale: bool,
}

impl TipLedger for Ledger {
    fn observe(&mut self, tip: &str, _known: Option<&str>, now: i64) -> Result<i64, String> {
        if self.stale {
            return Err("stale".into());
        }
        if !self.tips.iter().any(|t| t == tip) {
            self.tips.push(tip.into());
        }
        Ok(now - 5)
    }
    fn last(&mut self) -> Option<String> {
        self.tips.last().cloned()
    }
}

#[test]
fn a_stable_read_answers_with_evidence() {
    let mut source = Fake::new(vec!["a"], vec![vec![green_run(1, "push")]]);
    let answer = observe(&mut source, &mut Ledger::default(), &matrix());
    assert_eq!(answer.state, State::Green);
    assert_eq!(answer.tip, "a");
    assert_eq!(answer.runs.len(), 1);
}

#[test]
fn a_ref_that_moves_between_the_reads_starts_over_and_settles() {
    // A pass reads the tip before and after the listing: a, b fails; b, b settles.
    let mut source = Fake::new(vec!["a"], vec![vec![green_run(1, "push")]]);
    source.tips = vec![
        Ok("a".into()),
        Ok("b".into()),
        Ok("b".into()),
        Ok("b".into()),
    ];
    let answer = observe(&mut source, &mut Ledger::default(), &matrix());
    assert_eq!(answer.tip, "b");
    assert_eq!(answer.state, State::Green);
}

#[test]
fn three_moving_or_partial_passes_answer_state_unknown() {
    let mut source = Fake::new(vec!["a"], vec![vec![]]);
    source.runs = vec![Err("HTTP 502".into())];
    let answer = observe(&mut source, &mut Ledger::default(), &matrix());
    match answer.state {
        State::StateUnknown { failing_read } => assert!(failing_read.contains("list runs")),
        other => panic!("{other:?}"),
    }
    let mut moving = Fake::new(vec!["a"], vec![vec![green_run(1, "push")]]);
    moving.tips = (0..20).map(|i| Ok(format!("t{i}"))).collect();
    assert!(matches!(
        observe(&mut moving, &mut Ledger::default(), &matrix()).state,
        State::StateUnknown { .. }
    ));
}

#[test]
fn a_scheduled_run_that_completes_during_the_read_restarts_the_pass() {
    let queued = run(
        2,
        "schedule",
        vec![attempt(1, "in_progress", None, 5, "hosted", vec![])],
    );
    let mut jobs = green_jobs("hosted");
    jobs[0] = job("rust", "failure");
    let failed = run(
        2,
        "schedule",
        vec![attempt(1, "completed", Some("failure"), 5, "hosted", jobs)],
    );
    let mut source = Fake::new(
        vec!["a"],
        vec![
            vec![green_run(1, "push"), queued],
            vec![green_run(1, "push"), failed.clone()],
            vec![green_run(1, "push"), failed],
        ],
    );
    let answer = observe(&mut source, &mut Ledger::default(), &matrix());
    assert!(
        matches!(answer.state, State::Red { .. }),
        "{:?}",
        answer.state
    );
}

#[test]
fn an_unprotected_ref_answers_at_once_and_a_stale_ledger_call_is_never_green() {
    let mut source = Fake::new(vec!["a"], vec![vec![green_run(1, "push")]]);
    source.forward = Ok(false);
    assert_eq!(
        observe(&mut source, &mut Ledger::default(), &matrix()).state,
        State::RefNotForwardOnly
    );
    let mut stale = Ledger {
        stale: true,
        ..Default::default()
    };
    let mut source = Fake::new(vec!["a"], vec![vec![green_run(1, "push")]]);
    assert!(matches!(
        observe(&mut source, &mut stale, &matrix()).state,
        State::StateUnknown { .. }
    ));
}
