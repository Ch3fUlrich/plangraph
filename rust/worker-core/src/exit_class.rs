//! Classify how an attempt ended from observed signals.

use regex::RegexSet;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExitClass {
    Ok,
    CheckFail,
    FlakyCheck,
    UsageLimit,
    Outage,
    LeaseLost,
    WorkerCrash,
    Stall,
    Timeout,
    RebaseConflict,
    MergeRefused,
    ProbeFail,
    ContractDefect,
    NeedsInput,
    Cancelled,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CheckOutcome {
    Pass,
    Fail,
    /// Failed, then passed on rerun without changes.
    Flaky,
}

/// Everything the worker observed about one attempt.
#[derive(Debug, Clone, Default)]
pub struct Signals {
    /// Agent process exit code, if it exited normally.
    pub exit_code: Option<i32>,
    /// Terminating signal, if it was killed.
    pub signal: Option<i32>,
    pub check: Option<CheckOutcome>,
    pub timed_out: bool,
    pub stalled: bool,
    pub cancelled: bool,
    pub lease_lost: bool,
    pub contract_defect: bool,
    pub needs_input: bool,
    pub rebase_conflict: bool,
    pub merge_refused: bool,
    pub probe_failed: bool,
    pub stderr: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Classification {
    pub class: ExitClass,
    pub context_overflow: bool,
}

/// Configurable stderr matchers.
#[derive(Debug, Clone)]
pub struct Patterns {
    usage_limit: RegexSet,
    outage: RegexSet,
    context_overflow: RegexSet,
}

impl Patterns {
    pub fn new<S: AsRef<str>>(
        usage_limit: &[S],
        outage: &[S],
        context_overflow: &[S],
    ) -> Result<Self, regex::Error> {
        Ok(Patterns {
            usage_limit: RegexSet::new(usage_limit)?,
            outage: RegexSet::new(outage)?,
            context_overflow: RegexSet::new(context_overflow)?,
        })
    }
}

impl Default for Patterns {
    fn default() -> Self {
        Patterns::new(
            &[
                r"(?i)\b429\b",
                r"(?i)rate.?limit",
                r"(?i)quota",
                r"(?i)usage limit",
                r"(?i)too many requests",
            ],
            &[
                r"\b5\d\d\b.*(error|unavailable|gateway|overloaded)",
                r"(?i)(error|status|http)\D{0,4}\b5\d\d\b",
                r"(?i)connection (refused|reset)",
                r"(?i)service unavailable",
                r"(?i)overloaded",
                r"(?i)econnrefused|econnreset",
            ],
            &[
                r"(?i)context (length|window)",
                r"(?i)prompt is too long",
                r"(?i)maximum context",
                r"(?i)context_length_exceeded",
                r"(?i)too many tokens",
            ],
        )
        .expect("default patterns compile")
    }
}

impl Patterns {
    /// Decide the exit class. Explicit orchestration flags win, then
    /// timeouts/stalls, then stderr patterns for failed processes, then the check.
    pub fn classify(&self, s: &Signals) -> Classification {
        let failed_process = s.signal.is_some() || s.exit_code.is_some_and(|c| c != 0);
        let overflow = failed_process && self.context_overflow.is_match(&s.stderr);
        let class = if s.cancelled {
            ExitClass::Cancelled
        } else if s.lease_lost {
            ExitClass::LeaseLost
        } else if s.contract_defect {
            ExitClass::ContractDefect
        } else if s.needs_input {
            ExitClass::NeedsInput
        } else if s.rebase_conflict {
            ExitClass::RebaseConflict
        } else if s.merge_refused {
            ExitClass::MergeRefused
        } else if s.probe_failed {
            ExitClass::ProbeFail
        } else if s.timed_out {
            ExitClass::Timeout
        } else if s.stalled {
            ExitClass::Stall
        } else if failed_process && self.usage_limit.is_match(&s.stderr) {
            ExitClass::UsageLimit
        } else if failed_process && self.outage.is_match(&s.stderr) {
            ExitClass::Outage
        } else if failed_process {
            ExitClass::WorkerCrash
        } else {
            match s.check {
                Some(CheckOutcome::Fail) => ExitClass::CheckFail,
                Some(CheckOutcome::Flaky) => ExitClass::FlakyCheck,
                Some(CheckOutcome::Pass) => ExitClass::Ok,
                // Clean exit but no check result: the worker lost track.
                None => ExitClass::WorkerCrash,
            }
        };
        Classification {
            class,
            context_overflow: overflow,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn base() -> Signals {
        Signals {
            exit_code: Some(0),
            check: Some(CheckOutcome::Pass),
            ..Signals::default()
        }
    }

    fn with(f: impl FnOnce(&mut Signals)) -> Signals {
        let mut s = base();
        f(&mut s);
        s
    }

    #[test]
    fn classification_table() {
        use ExitClass::*;
        let crash = |err: &str| {
            with(|s| {
                s.exit_code = Some(1);
                s.stderr = err.into();
            })
        };
        let cases: Vec<(&str, Signals, ExitClass)> = vec![
            ("clean pass", base(), Ok),
            (
                "check fail",
                with(|s| s.check = Some(CheckOutcome::Fail)),
                CheckFail,
            ),
            (
                "flaky",
                with(|s| s.check = Some(CheckOutcome::Flaky)),
                FlakyCheck,
            ),
            ("no check result", with(|s| s.check = None), WorkerCrash),
            ("429", crash("HTTP 429 from provider"), UsageLimit),
            ("rate limit", crash("Rate-limit exceeded"), UsageLimit),
            ("rate limit spaced", crash("rate limit reached"), UsageLimit),
            (
                "quota",
                crash("You exceeded your current quota"),
                UsageLimit,
            ),
            ("503", crash("error: 503 Service Unavailable"), Outage),
            ("502", crash("upstream returned status 502"), Outage),
            ("refused", crash("connect: Connection refused"), Outage),
            ("overloaded", crash("API is overloaded"), Outage),
            ("unknown crash", crash("segfault"), WorkerCrash),
            (
                "500 lines is not an outage",
                crash("wrote 500 lines"),
                WorkerCrash,
            ),
            (
                "killed by signal",
                with(|s| {
                    s.exit_code = None;
                    s.signal = Some(9);
                }),
                WorkerCrash,
            ),
            (
                "limit text but exit 0",
                with(|s| s.stderr = "429 rate limit".into()),
                Ok,
            ),
            ("timeout", with(|s| s.timed_out = true), Timeout),
            ("stall", with(|s| s.stalled = true), Stall),
            (
                "timeout beats stall",
                with(|s| {
                    s.timed_out = true;
                    s.stalled = true;
                }),
                Timeout,
            ),
            (
                "stall beats stderr",
                with(|s| {
                    s.stalled = true;
                    s.stderr = "429".into();
                    s.signal = Some(15);
                }),
                Stall,
            ),
            ("cancelled", with(|s| s.cancelled = true), Cancelled),
            (
                "cancel beats lease",
                with(|s| {
                    s.cancelled = true;
                    s.lease_lost = true;
                }),
                Cancelled,
            ),
            ("lease lost", with(|s| s.lease_lost = true), LeaseLost),
            (
                "contract",
                with(|s| s.contract_defect = true),
                ContractDefect,
            ),
            ("needs input", with(|s| s.needs_input = true), NeedsInput),
            ("rebase", with(|s| s.rebase_conflict = true), RebaseConflict),
            (
                "merge refused",
                with(|s| s.merge_refused = true),
                MergeRefused,
            ),
            ("probe fail", with(|s| s.probe_failed = true), ProbeFail),
        ];
        let p = Patterns::default();
        for (name, sig, want) in cases {
            assert_eq!(p.classify(&sig).class, want, "{name}");
        }
    }

    #[test]
    fn context_overflow_flag() {
        let p = Patterns::default();
        let mut s = base();
        s.exit_code = Some(1);
        s.stderr = "Error: prompt is too long: 250000 tokens".into();
        let c = p.classify(&s);
        assert!(c.context_overflow);
        assert_eq!(c.class, ExitClass::WorkerCrash);
        s.stderr = "all fine".into();
        assert!(!p.classify(&s).context_overflow);
        s.exit_code = Some(0);
        s.stderr = "context window".into();
        assert!(!p.classify(&s).context_overflow);
    }

    #[test]
    fn custom_patterns() {
        let p = Patterns::new(&["^BUSY$"], &["^DOWN$"], &["^FULL$"]).unwrap();
        let mut s = base();
        s.exit_code = Some(2);
        s.stderr = "DOWN".into();
        assert_eq!(p.classify(&s).class, ExitClass::Outage);
        s.stderr = "BUSY".into();
        assert_eq!(p.classify(&s).class, ExitClass::UsageLimit);
        s.stderr = "429".into();
        assert_eq!(p.classify(&s).class, ExitClass::WorkerCrash);
        assert!(Patterns::new(&["("], &[], &[]).is_err());
    }

    #[test]
    fn serde_snake_case() {
        let j = serde_json::to_string(&ExitClass::RebaseConflict).unwrap();
        assert_eq!(j, "\"rebase_conflict\"");
        let back: ExitClass = serde_json::from_str("\"usage_limit\"").unwrap();
        assert_eq!(back, ExitClass::UsageLimit);
    }
}
