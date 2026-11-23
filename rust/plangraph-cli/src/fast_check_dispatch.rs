//! Fast-check placement and the one busy-host retry.
//!
//! A host is eligible when its available memory reaches its floor and its one-minute
//! load per CPU is below `MAX_LOAD_PER_CPU`. The least loaded eligible host runs the
//! checks. A failing run whose output carries the `host_busy` reason line (printed by
//! `tools/preflight.py` when an affected-test timeout fired on a loaded host) is retried
//! once on another eligible host; any other failure is a real test failure and final.
use serde::{Deserialize, Serialize};
use serde_json::Value;

/// Load per CPU at or above this value marks a host busy. `tools/ci_select.py` holds the
/// same number as `HOST_BUSY_LOAD_PER_CPU`; a test keeps the two equal.
pub const MAX_LOAD_PER_CPU: f64 = 0.7;
/// The machine-readable reason a busy-host timeout prints as a JSON line.
pub const HOST_BUSY: &str = "host_busy";
/// One run plus one retry.
const MAX_ATTEMPTS: usize = 2;

#[derive(Debug, Clone)]
pub struct Candidate {
    pub name: String,
    /// SSH destination; `None` runs on this host.
    pub route: Option<String>,
    /// Checkout the command runs in.
    pub cwd: String,
    pub mem_floor_mb: u64,
}

#[derive(Debug, Clone)]
pub struct Sample {
    pub mem_available_mb: u64,
    pub cpus: usize,
    pub load1: Option<f64>,
}

#[derive(Debug, Clone)]
pub struct Outcome {
    pub code: i32,
    pub output: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Verdict {
    Pass,
    HostBusy,
    TestFailure,
}

#[derive(Debug, Clone)]
pub struct Attempt {
    pub host: String,
    pub verdict: Verdict,
}

#[derive(Debug)]
pub struct Done {
    pub verdict: Verdict,
    pub code: i32,
    pub output: String,
    pub attempts: Vec<Attempt>,
}

/// Capacity probe and command runner of the hosts a dispatch may use.
pub trait Fleet {
    fn sample(&mut self, host: &Candidate) -> Option<Sample>;
    fn run(&mut self, host: &Candidate) -> Outcome;
}

/// One-minute load divided by the available CPUs; `None` when either is unknown.
pub fn load_per_cpu(sample: &Sample) -> Option<f64> {
    let load = sample.load1.filter(|l| l.is_finite() && *l >= 0.0)?;
    (sample.cpus > 0).then(|| load / sample.cpus as f64)
}

/// Why a host may not run the checks; `None` means eligible. An unknown load is never eligible.
fn rejection(host: &Candidate, sample: &Sample) -> Option<String> {
    if sample.mem_available_mb < host.mem_floor_mb {
        return Some(format!(
            "MemAvailable {} MB < floor {} MB",
            sample.mem_available_mb, host.mem_floor_mb
        ));
    }
    match load_per_cpu(sample) {
        None => Some("load unknown".into()),
        Some(load) if load >= MAX_LOAD_PER_CPU => {
            Some(format!("load {load:.2} per cpu >= {MAX_LOAD_PER_CPU}"))
        }
        Some(_) => None,
    }
}

#[cfg(test)]
fn eligible(host: &Candidate, sample: &Sample) -> bool {
    rejection(host, sample).is_none()
}

/// Success, a failure that carries the `host_busy` reason line, or a real test failure.
pub fn classify(outcome: &Outcome) -> Verdict {
    if outcome.code == 0 {
        return Verdict::Pass;
    }
    let busy = outcome.output.lines().any(|line| {
        serde_json::from_str::<Value>(line.trim())
            .ok()
            .and_then(|v| {
                v.get("reason")
                    .and_then(Value::as_str)
                    .map(|r| r == HOST_BUSY)
            })
            .unwrap_or(false)
    });
    if busy {
        Verdict::HostBusy
    } else {
        Verdict::TestFailure
    }
}

/// The least loaded eligible host outside `excluded`, or every rejection reason.
fn place<'a>(
    fleet: &mut dyn Fleet,
    hosts: &'a [Candidate],
    excluded: &[String],
) -> Result<&'a Candidate, String> {
    let mut best: Option<(f64, &Candidate)> = None;
    let mut reasons = Vec::new();
    for host in hosts.iter().filter(|h| !excluded.contains(&h.name)) {
        let Some(sample) = fleet.sample(host) else {
            reasons.push(format!("{}: no probe", host.name));
            continue;
        };
        if let Some(reason) = rejection(host, &sample) {
            reasons.push(format!("{}: {reason}", host.name));
        } else {
            let load = load_per_cpu(&sample).unwrap_or(f64::MAX);
            if best.is_none_or(|(b, _)| load < b) {
                best = Some((load, host));
            }
        }
    }
    best.map(|(_, h)| h).ok_or_else(|| {
        format!(
            "no eligible host for fast checks ({})",
            if reasons.is_empty() {
                "none left to try".into()
            } else {
                reasons.join("; ")
            }
        )
    })
}

/// Run on the best host; retry a busy-host failure once on another eligible host.
pub fn dispatch(fleet: &mut dyn Fleet, hosts: &[Candidate]) -> Result<Done, String> {
    let mut attempts: Vec<Attempt> = Vec::new();
    let mut excluded: Vec<String> = Vec::new();
    let mut busy: Option<Outcome> = None;
    loop {
        let host = match place(fleet, hosts, &excluded) {
            Ok(host) => host,
            // A retry with no other eligible host ends on the busy result already in hand.
            Err(_) if busy.is_some() => {
                let outcome = busy.take().unwrap_or(Outcome {
                    code: 1,
                    output: String::new(),
                });
                return Ok(Done {
                    verdict: Verdict::HostBusy,
                    code: outcome.code,
                    output: outcome.output,
                    attempts,
                });
            }
            Err(error) => return Err(error),
        };
        let outcome = fleet.run(host);
        let verdict = classify(&outcome);
        attempts.push(Attempt {
            host: host.name.clone(),
            verdict,
        });
        excluded.push(host.name.clone());
        if verdict == Verdict::HostBusy && attempts.len() < MAX_ATTEMPTS {
            busy = Some(outcome);
            continue;
        }
        return Ok(Done {
            verdict,
            code: outcome.code,
            output: outcome.output,
            attempts,
        });
    }
}

/// Attempt verdict counts of a dispatch log, one JSON object per line.
#[derive(Debug, Default, PartialEq, Eq, Serialize)]
pub struct Counts {
    pub passed: u64,
    pub host_busy: u64,
    pub test_failure: u64,
    pub ignored: u64,
}

pub fn summarize(log: &str) -> Counts {
    let mut counts = Counts::default();
    for line in log.lines().filter(|l| !l.trim().is_empty()) {
        let verdict = serde_json::from_str::<Value>(line)
            .ok()
            .and_then(|v| serde_json::from_value::<Verdict>(v.get("verdict")?.clone()).ok());
        match verdict {
            Some(Verdict::Pass) => counts.passed += 1,
            Some(Verdict::HostBusy) => counts.host_busy += 1,
            Some(Verdict::TestFailure) => counts.test_failure += 1,
            None => counts.ignored += 1,
        }
    }
    counts
}

#[cfg(test)]
#[path = "fast_check_dispatch_tests.rs"]
mod tests;
