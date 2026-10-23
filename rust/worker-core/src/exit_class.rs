//! Classify how an attempt ended from observed signals.

use regex::RegexSet;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExitClass {
    Ok,
    CheckFail,
    /// Exited cleanly with a passing (or absent) check but changed nothing.
    NoChange,
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
    /// The provider reported a rate limit or exhausted quota.
    pub provider_rate_limited: bool,
    /// The provider reported any other error (transport, unknown, ...).
    pub provider_error: bool,
    /// The harness produced no output at all on a clean exit.
    pub empty_output: bool,
    /// Exited cleanly but left the worktree unchanged: nothing to merge.
    pub no_change: bool,
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
                // Claude CLI: subscription window and prepaid credit exhaustion.
                r"(?i)5-hour limit reached",
                r"(?i)usage limit reached",
                r"(?i)credit balance is too low",
            ],
            &[
                r"\b5\d\d\b.*(error|unavailable|gateway|overloaded)",
                r"(?i)(error|status|http)\D{0,4}\b5\d\d\b",
                r"(?i)connection (refused|reset)",
                r"(?i)service unavailable",
                r"(?i)overloaded",
                r"(?i)overloaded_error",
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
    /// timeouts/stalls, then the provider's own error stream (a provider error
    /// is never `ok`, whatever the harness exit code says), then stderr
    /// patterns for failed processes, then a no-op run, then the check.
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
        } else if s.provider_rate_limited {
            // The provider's own event stream outranks the exit code: a quota
            // error that still exits 0 is a limit, not success.
            ExitClass::UsageLimit
        } else if s.provider_error || s.empty_output {
            // A broken provider, or a silent run that never got going, is the
            // provider's fault whatever the exit code says.
            ExitClass::Outage
        } else if failed_process && self.usage_limit.is_match(&s.stderr) {
            ExitClass::UsageLimit
        } else if failed_process && self.outage.is_match(&s.stderr) {
            ExitClass::Outage
        } else if failed_process {
            ExitClass::WorkerCrash
        } else if s.no_change {
            // A clean run that changed nothing did not do the work. It must not
            // reach `ok` (nothing to merge) nor a check verdict the check never
            // reached, so it gets its own class before the check match.
            ExitClass::NoChange
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
    fn trailing_newline_and_mixed_patterns_still_classify() {
        let crash = |err: &str| {
            with(|s| {
                s.exit_code = Some(1);
                s.stderr = err.into();
            })
        };
        let p = Patterns::default();
        assert_eq!(
            p.classify(&crash("error: 429\n")).class,
            ExitClass::UsageLimit
        );
        assert_eq!(p.classify(&crash("429")).class, ExitClass::UsageLimit);
        // usage_limit and outage are handled identically by the budget
        // (re-route, then wait), so their relative precedence is not observable.
        assert_eq!(
            p.classify(&crash("503 service unavailable, then 429\n"))
                .class,
            ExitClass::UsageLimit
        );
    }

    /// The Claude limit patterns are phrases, not the bare word "usage": CLI
    /// usage text and a check whose output merely mentions usage stay what
    /// they are.
    #[test]
    fn the_word_usage_alone_is_not_a_limit() {
        let p = Patterns::default();
        // A failing check (harness exit 0) whose output says "usage".
        let mut s = base();
        s.check = Some(CheckOutcome::Fail);
        s.stderr = "usage: tool [options]\nerror: bad flag; usage of memory high".into();
        assert_eq!(p.classify(&s).class, ExitClass::CheckFail);
        // A crashing harness printing its own usage banner is a crash.
        let mut s = base();
        s.exit_code = Some(2);
        s.stderr = "Usage: claude [options] [prompt]\nerror: unknown option".into();
        assert_eq!(p.classify(&s).class, ExitClass::WorkerCrash);
        // The real thing still matches.
        s.stderr = "Claude usage limit reached. Try again later".into();
        assert_eq!(p.classify(&s).class, ExitClass::UsageLimit);
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
            (
                "claude 5-hour limit",
                crash("Claude AI usage limit: 5-hour limit reached, resets 3am"),
                UsageLimit,
            ),
            (
                "claude usage limit",
                crash("Claude usage limit reached. Try again later"),
                UsageLimit,
            ),
            (
                "claude overloaded_error",
                crash(r#"{"type":"error","error":{"type":"overloaded_error"}}"#),
                Outage,
            ),
            (
                "claude credit balance",
                crash("Credit balance is too low to access the API"),
                UsageLimit,
            ),
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

    /// A provider error or an empty run is never `ok`, even when the harness
    /// exits 0 and the check passed.
    #[test]
    fn provider_signals_and_no_change_outrank_a_clean_exit() {
        let p = Patterns::default();
        let cases = [
            (
                "rate limited",
                with(|s| s.provider_rate_limited = true),
                ExitClass::UsageLimit,
            ),
            (
                "provider error",
                with(|s| s.provider_error = true),
                ExitClass::Outage,
            ),
            (
                "empty output",
                with(|s| s.empty_output = true),
                ExitClass::Outage,
            ),
        ];
        for (name, sig, want) in cases {
            assert_eq!(sig.exit_code, Some(0));
            assert_eq!(sig.check, Some(CheckOutcome::Pass));
            assert_eq!(p.classify(&sig).class, want, "{name}");
        }
        assert_eq!(
            p.classify(&with(|s| s.no_change = true)).class,
            ExitClass::NoChange
        );
        // A crashing harness also changed nothing, but the crash explains it.
        let mut crashed = with(|s| s.no_change = true);
        crashed.exit_code = Some(1);
        assert_eq!(p.classify(&crashed).class, ExitClass::WorkerCrash);
        // The all-clear case is still Ok.
        assert_eq!(p.classify(&base()).class, ExitClass::Ok);
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
        let j = serde_json::to_string(&ExitClass::NoChange).unwrap();
        assert_eq!(j, "\"no_change\"");
        let back: ExitClass = serde_json::from_str("\"no_change\"").unwrap();
        assert_eq!(back, ExitClass::NoChange);
    }
}
