//! The service steps of the batch step list: claim (keeper `claim-batch`), ci_class (the CI
//! run of the pushed head, classified) and train_join (the landing train desk). Each is a
//! narrow trait so a fake keeper, CI or train runs the whole list in a test; the command
//! backed implementations are in `step_services_cli`. The other six steps are passed on to
//! `rest`, the way `Composite` passes the agent steps on.

use crate::step_runner::StepActions;
use pg_worker_core::batch_steps::{
    CiClass, Claim, Classification, FastChecks, Fix, Push, Review, TrainJoin, Worktree, Write,
};
use std::time::Duration;

/// The keeper calls of a batch claim.
pub trait Keeper {
    /// Claim every key with one shared lease; `Ok` holds `(key, epoch)` in request order and an
    /// `Err` means no key stays claimed.
    fn claim_batch(&mut self, keys: &[String]) -> Result<Vec<(String, u64)>, String>;
}

/// One workflow run of a head, as the CI service reports it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CiRun {
    /// The workflow's name.
    pub name: String,
    /// The event that started the run (`pull_request`, `workflow_dispatch`, `push`...): runs of
    /// different events are independent evidence for one head.
    pub event: String,
    pub id: u64,
    pub attempt: u32,
    /// `queued`, `in_progress` or `completed`.
    pub status: String,
    /// Set once the run completed.
    pub conclusion: Option<String>,
}

pub trait CiRuns {
    /// Every workflow run of exactly `head` that belongs to pull request `pr`, latest attempt
    /// of each.
    fn runs(&mut self, head: &str, pr: u64) -> Result<Vec<CiRun>, String>;
    /// Whether `pr`, with `head` as its current head, is a draft pull request from a `lane/*`
    /// branch: the part whose workflow jobs are skipped by design. A pull request whose head
    /// moved on is not asked about the old head.
    fn draft_lane_part(&mut self, pr: u64, head: &str) -> Result<bool, String>;
}

/// The landing train desk: registers the green part and returns the train it joined.
pub trait TrainDesk {
    fn join(&mut self, head: &str, pr: u64, run: u64, attempt: u32) -> Result<TrainJoin, String>;
}

/// How long `ci_class` waits for the runs of a head to finish: `polls` looks, `interval` apart.
#[derive(Clone, Copy, Debug)]
pub struct CiWait {
    pub polls: u32,
    pub interval: Duration,
}

impl Default for CiWait {
    fn default() -> Self {
        Self {
            polls: 120,
            interval: Duration::from_secs(30),
        }
    }
}

/// What the runs of a head say so far.
#[derive(Debug)]
pub enum Reading {
    /// No run yet, or one that has not completed.
    Pending(&'static str),
    /// Every run finished and none failed, yet a required workflow has no passing run: a skipped
    /// workflow is no evidence for a train, and waiting longer will not change it.
    Unproven(String),
    Done(CiClass),
}

/// Conclusions that do not make a head red: the run did what it was asked to.
const PASSING: [&str; 3] = ["success", "skipped", "neutral"];

/// The runs that count: the latest run of each workflow and event. An older run of the same
/// workflow and event (an earlier push) is history, but a run of another event is independent
/// evidence for the head and never hides it (the CI watcher selects per head and event too).
fn current(runs: &[CiRun]) -> Vec<&CiRun> {
    let mut latest: Vec<&CiRun> = Vec::new();
    for run in runs {
        let same = |seen: &&mut &CiRun| seen.name == run.name && seen.event == run.event;
        match latest.iter_mut().find(same) {
            Some(seen) if seen.id < run.id => *seen = run,
            Some(_) => {}
            None => latest.push(run),
        }
    }
    latest
}

/// Green when every current run completed without a failure; red as soon as one completed with
/// one, naming that run, so the autofix reads the run that failed. A head without runs is
/// pending, never green.
///
/// `draft_part` is the policy of a draft `lane/*` pull request: it runs no job (its jobs are
/// skipped, a skipped required check reports success; admission is review and the fast gates
/// on the writer host), so a skipped required workflow is the passing evidence there. For any
/// other pull request a required workflow needs a run that succeeded.
pub fn classify(runs: &[CiRun], required: &[String], draft_part: bool) -> Reading {
    let runs = current(runs);
    if runs.is_empty() {
        return Reading::Pending("no CI run for the head yet");
    }
    let conclusion = |run: &CiRun| {
        (run.status == "completed")
            .then(|| run.conclusion.as_deref().unwrap_or("unknown"))
            .map(str::to_owned)
    };
    let red = runs
        .iter()
        .filter(|run| conclusion(run).is_some_and(|text| !PASSING.contains(&text.as_str())))
        .max_by_key(|run| run.id);
    if let Some(run) = red {
        return Reading::Done(CiClass {
            classification: Classification::Red,
            run: run.id,
            attempt: run.attempt,
        });
    }
    if runs.iter().any(|run| conclusion(run).is_none()) {
        return Reading::Pending("a CI run has not completed");
    }
    // Green speaks for the required workflows: each has current runs and every one passed. The
    // skipped run of a draft lane part counts only for its own `pull_request` event.
    for name in required {
        let of_name: Vec<&&CiRun> = runs.iter().filter(|run| &run.name == name).collect();
        if of_name.is_empty() {
            return Reading::Pending("a required workflow has no run for the head yet");
        }
        let passed = |run: &CiRun| match run.conclusion.as_deref() {
            Some("success") => true,
            Some("skipped" | "neutral") => draft_part && run.event == "pull_request",
            _ => false,
        };
        if !of_name.iter().all(|run| passed(run)) {
            return Reading::Unproven(format!("required workflow {name} has no passing run"));
        }
    }
    let last = runs
        .iter()
        .filter(|run| required.is_empty() || required.contains(&run.name))
        .max_by_key(|run| run.id)
        .expect("runs not empty");
    Reading::Done(CiClass {
        classification: Classification::Green,
        run: last.id,
        attempt: last.attempt,
    })
}

pub struct ServiceSteps<K: Keeper, C: CiRuns, T: TrainDesk, A: StepActions> {
    pub keeper: K,
    pub ci: C,
    pub train: T,
    pub wait: CiWait,
    /// Workflow names that must have passed for the head to be green.
    pub required: Vec<String>,
    /// Called between two looks at the CI; a test passes a no-op.
    pub sleep: Box<dyn FnMut(Duration)>,
    pub rest: A,
}

impl<K: Keeper, C: CiRuns, T: TrainDesk, A: StepActions> StepActions for ServiceSteps<K, C, T, A> {
    fn claim(&mut self, items: &[String]) -> Result<Claim, String> {
        let held = self.keeper.claim_batch(items)?;
        let keys: Vec<&str> = held.iter().map(|(key, _)| key.as_str()).collect();
        if keys != items.iter().map(String::as_str).collect::<Vec<_>>() {
            return Err(format!("keeper claimed {keys:?}, not {items:?}"));
        }
        let epochs: Vec<String> = held
            .iter()
            .map(|(key, epoch)| format!("{key}@{epoch}"))
            .collect();
        Ok(Claim {
            claim: epochs.join(","),
        })
    }
    fn ci_class(&mut self, head: &str, pr: u64) -> Result<CiClass, String> {
        let mut last = "no CI look taken";
        for look in 0..self.wait.polls.max(1) {
            if look > 0 {
                (self.sleep)(self.wait.interval);
            }
            let runs = self.ci.runs(head, pr)?;
            // Asked of the pull request itself each look: a reused or re-marked PR may not be
            // the draft part the branch name suggests.
            let draft_part = self.ci.draft_lane_part(pr, head)?;
            match classify(&runs, &self.required, draft_part) {
                Reading::Done(class) => return Ok(class),
                Reading::Unproven(why) => return Err(format!("CI of {head}: {why}")),
                Reading::Pending(why) => last = why,
            }
        }
        Err(format!(
            "CI of {head} not finished after {} looks: {last}",
            self.wait.polls.max(1)
        ))
    }
    fn train_join(
        &mut self,
        head: &str,
        pr: u64,
        run: u64,
        attempt: u32,
    ) -> Result<TrainJoin, String> {
        self.train.join(head, pr, run, attempt)
    }
    fn worktree(&mut self, claim: &str, base: &str) -> Result<Worktree, String> {
        self.rest.worktree(claim, base)
    }
    fn write(&mut self, worktree: &str, brief: &str) -> Result<Write, String> {
        self.rest.write(worktree, brief)
    }
    fn fast_checks(&mut self, head: &str) -> Result<FastChecks, String> {
        self.rest.fast_checks(head)
    }
    fn review(&mut self, head: &str, stamp: &str) -> Result<Review, String> {
        self.rest.review(head, stamp)
    }
    fn fix(&mut self, head: &str, session: &str, review: &str) -> Result<Fix, String> {
        self.rest.fix(head, session, review)
    }
    fn push(&mut self, head: &str, stamp: &str, review: &str) -> Result<Push, String> {
        self.rest.push(head, stamp, review)
    }
}

/// A boxed set of actions is a set of actions, so the host builds one without its type.
impl<T: StepActions + ?Sized> StepActions for Box<T> {
    fn claim(&mut self, items: &[String]) -> Result<Claim, String> {
        (**self).claim(items)
    }
    fn worktree(&mut self, claim: &str, base: &str) -> Result<Worktree, String> {
        (**self).worktree(claim, base)
    }
    fn write(&mut self, worktree: &str, brief: &str) -> Result<Write, String> {
        (**self).write(worktree, brief)
    }
    fn fast_checks(&mut self, head: &str) -> Result<FastChecks, String> {
        (**self).fast_checks(head)
    }
    fn review(&mut self, head: &str, stamp: &str) -> Result<Review, String> {
        (**self).review(head, stamp)
    }
    fn fix(&mut self, head: &str, session: &str, review: &str) -> Result<Fix, String> {
        (**self).fix(head, session, review)
    }
    fn push(&mut self, head: &str, stamp: &str, review: &str) -> Result<Push, String> {
        (**self).push(head, stamp, review)
    }
    fn ci_class(&mut self, head: &str, pr: u64) -> Result<CiClass, String> {
        (**self).ci_class(head, pr)
    }
    fn train_join(
        &mut self,
        head: &str,
        pr: u64,
        run: u64,
        attempt: u32,
    ) -> Result<TrainJoin, String> {
        (**self).train_join(head, pr, run, attempt)
    }
}
