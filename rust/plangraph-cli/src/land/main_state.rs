//! The one definition of main's state (red-main design D2): green, pending, red or unknown.
//!
//! `evaluate` is pure over one consistent observation; `observe` reads the ref, the runs and the ref again
//! (three tries) and returns the answer with its evidence; `admit` is the check every merge path calls.
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

/// A run with no attempt counts as pending for this long after the tip was first seen (seconds).
pub const NO_PUSH_RUN_SECS: i64 = 15 * 60;
/// Lower bound of the stall bound T (seconds).
pub const MIN_STALL_SECS: i64 = 30 * 60;
/// The final listing is read at most this many times before the answer is `state_unknown`.
pub const TRIES: usize = 3;

/// One job of an attempt as GitHub lists it. `conclusion` is `None` while the job is not done.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Job {
    pub name: String,
    pub conclusion: Option<String>,
    /// The watcher could not read the whole log of this failed job (capped, truncated, died early).
    #[serde(default)]
    pub partial_log: bool,
    /// Failing test or check ids the watcher diagnosed in this job.
    #[serde(default)]
    pub tests: Vec<String>,
}

/// One attempt of a run: `status` is queued, in_progress or completed.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Attempt {
    pub number: u32,
    pub status: String,
    pub conclusion: Option<String>,
    /// Epoch seconds the attempt started (or was created while queued).
    pub started: i64,
    /// Runner tier the attempt ran on; selects the expected job matrix.
    pub tier: String,
    pub jobs: Vec<Job>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Run {
    pub id: u64,
    pub event: String,
    pub attempts: Vec<Attempt>,
}

/// The expected jobs per runner tier, each with the suite it belongs to.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Matrix {
    pub tiers: BTreeMap<String, BTreeMap<String, String>>,
}

impl Matrix {
    fn suite<'a>(&'a self, tier: &str, job: &'a str) -> Option<&'a str> {
        self.tiers.get(tier)?.get(job).map(String::as_str)
    }
}

/// An entry of a run's failure set.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub enum Failure {
    /// A failed suite, with the failing test ids the watcher recorded.
    Suite { suite: String, tests: Vec<String> },
    /// An attempt that failed without a usable job list; only a complete-green attempt clears it.
    Workflow { attempt: u32 },
    /// The diagnosis of a failed job of this suite may be partial.
    Incomplete { suite: String },
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Blocking {
    pub run: u64,
    pub event: String,
    pub attempt: u32,
    pub stalled: bool,
    pub failures: Vec<Failure>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "state", rename_all = "snake_case")]
pub enum State {
    Green,
    Pending,
    /// `run` 0 in a blocking entry means the push run does not exist (`none`).
    Red {
        blocking: Vec<Blocking>,
    },
    StateUnknown {
        failing_read: String,
    },
    RefNotForwardOnly,
}

impl State {
    /// The refusal word callers print, `None` when main is green.
    pub fn refusal(&self) -> Option<&'static str> {
        match self {
            State::Green => None,
            State::Pending => Some("main_pending"),
            State::Red { .. } => Some("main_red"),
            State::StateUnknown { .. } => Some("state_unknown"),
            State::RefNotForwardOnly => Some("ref_not_forward_only"),
        }
    }
}

/// The state with the evidence it was judged on.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Answer {
    pub tip: String,
    pub state: State,
    pub runs: Vec<Run>,
}

/// The bound T: twice the median duration of the last 20 green push runs, at least 30 minutes.
pub fn stall_bound(green_push_durations: &[i64]) -> i64 {
    let mut last: Vec<i64> = green_push_durations
        .iter()
        .rev()
        .take(20)
        .copied()
        .collect();
    last.sort_unstable();
    let median = match last.len() {
        0 => 0,
        n if n % 2 == 1 => last[n / 2],
        n => (last[n / 2 - 1] + last[n / 2]) / 2,
    };
    (2 * median).max(MIN_STALL_SECS)
}

fn expected_ok(matrix: &Matrix, attempt: &Attempt) -> bool {
    let Some(expected) = matrix.tiers.get(&attempt.tier) else {
        return false;
    };
    expected.keys().all(|name| {
        attempt
            .jobs
            .iter()
            .any(|j| &j.name == name && j.conclusion.as_deref() == Some("success"))
    })
}

fn complete_green(matrix: &Matrix, attempt: &Attempt) -> bool {
    attempt.status == "completed"
        && attempt.conclusion.as_deref() == Some("success")
        && expected_ok(matrix, attempt)
}

fn stalled(attempt: &Attempt, now: i64, t: i64) -> bool {
    attempt.status != "completed" && now - attempt.started >= t
}

/// Suites an attempt ran with every job of the suite succeeded.
fn green_suites(matrix: &Matrix, attempt: &Attempt) -> BTreeSet<String> {
    let mut seen: BTreeMap<&str, bool> = BTreeMap::new();
    for job in &attempt.jobs {
        if let Some(suite) = matrix.suite(&attempt.tier, &job.name) {
            let ok = job.conclusion.as_deref() == Some("success");
            let entry = seen.entry(suite).or_insert(true);
            *entry &= ok;
        }
    }
    seen.into_iter()
        .filter(|(_, ok)| *ok)
        .map(|(s, _)| s.to_owned())
        .collect()
}

/// An attempt counts as completed for the failure set when GitHub says so or it is stalled.
fn done(attempt: &Attempt, now: i64, t: i64) -> bool {
    attempt.status == "completed" || stalled(attempt, now, t)
}

/// The failure set of a run: entries of every completed attempt not discharged by a later attempt.
pub fn failure_set(matrix: &Matrix, run: &Run, now: i64, t: i64) -> Vec<Failure> {
    let mut attempts: Vec<&Attempt> = run.attempts.iter().collect();
    attempts.sort_by_key(|a| a.number);
    let mut out: BTreeSet<Failure> = BTreeSet::new();
    for (i, attempt) in attempts.iter().enumerate() {
        if !done(attempt, now, t) {
            continue;
        }
        let later: Vec<&&Attempt> = attempts[i + 1..].iter().collect();
        let clears_all = later
            .iter()
            .any(|a| a.status == "completed" && complete_green(matrix, a));
        let mut suites: BTreeMap<String, Vec<String>> = BTreeMap::new();
        let mut incomplete: BTreeSet<String> = BTreeSet::new();
        let mut failed_job = false;
        for job in &attempt.jobs {
            let bad = matches!(
                job.conclusion.as_deref(),
                Some("failure" | "timed_out" | "cancelled" | "startup_failure")
            );
            if !bad {
                continue;
            }
            failed_job = true;
            let suite = matrix
                .suite(&attempt.tier, &job.name)
                .unwrap_or(job.name.as_str())
                .to_owned();
            suites
                .entry(suite.clone())
                .or_default()
                .extend(job.tests.clone());
            if job.partial_log {
                incomplete.insert(suite);
            }
        }
        let healthy = attempt.status == "completed"
            && attempt.conclusion.as_deref() == Some("success")
            && expected_ok(matrix, attempt);
        if !failed_job && !healthy && !clears_all {
            // Failed, stalled or short of an expected job without a failed job on the list.
            out.insert(Failure::Workflow {
                attempt: attempt.number,
            });
        }
        for (suite, mut tests) in suites {
            let discharged = clears_all
                || later
                    .iter()
                    .any(|a| a.status == "completed" && green_suites(matrix, a).contains(&suite));
            if discharged {
                continue;
            }
            tests.sort();
            tests.dedup();
            out.insert(Failure::Suite {
                suite: suite.clone(),
                tests,
            });
            if incomplete.contains(&suite) {
                out.insert(Failure::Incomplete { suite });
            }
        }
    }
    out.into_iter().collect()
}

fn blocking_of(matrix: &Matrix, run: &Run, now: i64, t: i64) -> Option<Blocking> {
    let failures = failure_set(matrix, run, now, t);
    let latest_done = run
        .attempts
        .iter()
        .filter(|a| done(a, now, t))
        .max_by_key(|a| a.number)?;
    let green = latest_done.status == "completed" && complete_green(matrix, latest_done);
    if green && failures.is_empty() {
        return None;
    }
    Some(Blocking {
        run: run.id,
        event: run.event.clone(),
        attempt: latest_done.number,
        stalled: stalled(latest_done, now, t),
        failures,
    })
}

/// The state on one consistent observation. `first_seen` is when the tip was first observed (epoch seconds).
pub fn evaluate(matrix: &Matrix, runs: &[Run], first_seen: i64, now: i64, t: i64) -> State {
    let mut blocking: Vec<Blocking> = runs
        .iter()
        .filter_map(|r| blocking_of(matrix, r, now, t))
        .collect();
    let push = runs.iter().find(|r| r.event == "push");
    if push.is_none() && now - first_seen >= NO_PUSH_RUN_SECS {
        blocking.push(Blocking {
            run: 0,
            event: "push".into(),
            attempt: 0,
            stalled: false,
            failures: vec![],
        });
    }
    if !blocking.is_empty() {
        return State::Red { blocking };
    }
    let push_done = push.is_some_and(|r| r.attempts.iter().any(|a| done(a, now, t)));
    if push_done {
        State::Green
    } else {
        State::Pending
    }
}

/// Every read of one observation pass. Errors are partial reads; the pass starts over.
pub trait Source {
    fn forward_only(&mut self) -> Result<bool, String>;
    fn tip(&mut self) -> Result<String, String>;
    fn runs(&mut self, tip: &str) -> Result<Vec<Run>, String>;
    fn green_push_durations(&mut self) -> Result<Vec<i64>, String>;
    fn now(&mut self) -> i64;
}

/// The keeper's record of when a tip was first observed.
pub trait TipLedger {
    /// Records `tip` (first observation) given the last recorded tip the caller knows; returns its first-seen time.
    /// `Err` means the call was stale: the caller reads GitHub again.
    fn observe(&mut self, tip: &str, known_last: Option<&str>, now: i64) -> Result<i64, String>;
    fn last(&mut self) -> Option<String>;
}

fn signature(runs: &[Run]) -> Vec<(u64, u32, String)> {
    let mut sig: Vec<_> = runs
        .iter()
        .flat_map(|r| {
            r.attempts
                .iter()
                .map(move |a| (r.id, a.number, a.status.clone()))
        })
        .collect();
    sig.sort();
    sig
}

/// One bracketed pass: ref, listing, ref, listing again, ref. `Err` names the failing or moved read.
fn pass(
    source: &mut impl Source,
    ledger: &mut impl TipLedger,
) -> Result<(String, Vec<Run>, i64, i64), String> {
    let tip = source.tip().map_err(|e| format!("read ref: {e}"))?;
    let known = ledger.last();
    let now = source.now();
    let first_seen = ledger
        .observe(&tip, known.as_deref(), now)
        .map_err(|e| format!("tip record: {e}"))?;
    let first = source.runs(&tip).map_err(|e| format!("list runs: {e}"))?;
    let t = stall_bound(
        &source
            .green_push_durations()
            .map_err(|e| format!("durations: {e}"))?,
    );
    let second = source.runs(&tip).map_err(|e| format!("list runs: {e}"))?;
    let after = source.tip().map_err(|e| format!("read ref: {e}"))?;
    if after != tip {
        return Err("ref moved during the read".into());
    }
    if signature(&first) != signature(&second) {
        return Err("runs changed during the read".into());
    }
    Ok((tip, second, first_seen, t))
}

/// Main's state with evidence. A ref without forward-only protection answers at once; three failed or
/// moving passes answer `state_unknown`.
pub fn observe(source: &mut impl Source, ledger: &mut impl TipLedger, matrix: &Matrix) -> Answer {
    match source.forward_only() {
        Ok(true) => {}
        Ok(false) => {
            return Answer {
                tip: String::new(),
                state: State::RefNotForwardOnly,
                runs: vec![],
            }
        }
        Err(e) => {
            return unknown(format!("read protection: {e}"));
        }
    }
    let mut failing = String::new();
    for _ in 0..TRIES {
        match pass(source, ledger) {
            Ok((tip, runs, first_seen, t)) => {
                let now = source.now();
                let state = evaluate(matrix, &runs, first_seen, now, t);
                return Answer { tip, state, runs };
            }
            Err(e) => failing = e,
        }
    }
    unknown(failing)
}

fn unknown(failing_read: String) -> Answer {
    Answer {
        tip: String::new(),
        state: State::StateUnknown { failing_read },
        runs: vec![],
    }
}

/// The merge path's admission check. An ordinary merge needs green; `EXCEPTION:main-red-fix` is honoured
/// only while the state is red at this read.
pub fn admit(state: &State, exception: Option<&str>) -> Result<(), &'static str> {
    match state {
        State::Green => Ok(()),
        State::Red { .. } if exception == Some("EXCEPTION:main-red-fix") => Ok(()),
        other => Err(other.refusal().unwrap_or("main_red")),
    }
}
