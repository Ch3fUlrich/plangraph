//! The question packet a harness leaves when it needs a human, and the pure
//! decisions the workflow takes on it. No Temporal and no I/O here.
//!
//! A harness run that wants input prints one line `PG_QUESTION: {json}` and
//! stops. See `NEEDS_INPUT.md` for the whole flow.

use serde::{Deserialize, Serialize};
use std::time::Duration;

/// Output-line prefix that marks a question packet.
pub const QUESTION_PREFIX: &str = "PG_QUESTION:";

/// What the agent asks, with the optional fallback if nobody answers.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Question {
    pub text: String,
    #[serde(default)]
    pub default: Option<String>,
    #[serde(default)]
    pub deadline_s: Option<u64>,
    #[serde(default)]
    pub risk_tags: Vec<String>,
}

impl Question {
    /// The question as the keeper will store it. A default only survives when
    /// it can be used: it needs a deadline, and a risk-tagged question never
    /// has one (fail closed on any tag; the keeper refuses the sensitive ones).
    pub fn normalized(&self) -> Question {
        let usable = self.deadline_s.is_some() && self.risk_tags.is_empty();
        Question {
            text: self.text.clone(),
            default: if usable { self.default.clone() } else { None },
            deadline_s: self.deadline_s,
            risk_tags: self.risk_tags.clone(),
        }
    }
}

/// What the scan of a harness output found.
#[derive(Debug, Clone, PartialEq)]
pub enum Scan {
    /// No `PG_QUESTION:` line: the run did not ask anything.
    None,
    /// The last `PG_QUESTION:` line parsed.
    Asked(Question),
    /// A `PG_QUESTION:` line is there but unusable (the reason is the text).
    Malformed(String),
}

/// Find the last `PG_QUESTION:` line in `output` and parse it.
pub fn scan_output(output: &str) -> Scan {
    let Some(line) = output
        .lines()
        .rev()
        .map(str::trim_start)
        .find(|l| l.starts_with(QUESTION_PREFIX))
    else {
        return Scan::None;
    };
    let json = line[QUESTION_PREFIX.len()..].trim();
    match serde_json::from_str::<Question>(json) {
        Ok(q) if q.text.trim().is_empty() => Scan::Malformed("question text is empty".into()),
        Ok(q) => Scan::Asked(q),
        Err(e) => Scan::Malformed(format!("PG_QUESTION is not a valid packet: {e}")),
    }
}

/// What the workflow does when its wait timer fires unanswered.
#[derive(Debug, Clone, PartialEq)]
pub enum OnTimer {
    /// Record `question_timeout` and continue with this default text.
    UseDefault(String),
    /// Nothing to fall back on: park and alarm.
    Park,
}

/// How long to wait for an answer and what a silent timer means.
#[derive(Debug, Clone, PartialEq)]
pub struct WaitPlan {
    pub wait: Duration,
    pub on_timer: OnTimer,
}

/// Decide the wait for `q` (already normalized), bounded by `wait_cap`.
/// A default is taken only when its deadline falls inside the cap.
pub fn plan_wait(q: &Question, wait_cap: Duration) -> WaitPlan {
    if let (Some(default), Some(s)) = (&q.default, q.deadline_s) {
        let d = Duration::from_secs(s);
        if d <= wait_cap {
            return WaitPlan {
                wait: d,
                on_timer: OnTimer::UseDefault(default.clone()),
            };
        }
    }
    WaitPlan {
        wait: wait_cap,
        on_timer: OnTimer::Park,
    }
}

/// The prompt for an attempt: the claim prompt plus every answer so far.
pub fn prompt_with_answers(prompt: &str, answers: &[String]) -> String {
    let mut out = prompt.to_string();
    for a in answers {
        out.push_str("\n\nAnswer to your question: ");
        out.push_str(a);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn q(default: Option<&str>, deadline_s: Option<u64>, tags: &[&str]) -> Question {
        Question {
            text: "which db?".into(),
            default: default.map(str::to_string),
            deadline_s,
            risk_tags: tags.iter().map(|t| t.to_string()).collect(),
        }
    }

    #[test]
    fn scan_takes_the_last_question_line() {
        let out = "noise\nPG_QUESTION: {\"text\":\"a\"}\nmore\n  PG_QUESTION: {\"text\":\"b\",\"default\":\"x\",\"deadline_s\":5,\"risk_tags\":[]}\n";
        let Scan::Asked(got) = scan_output(out) else {
            panic!("expected a question");
        };
        let mut want = q(Some("x"), Some(5), &[]);
        want.text = "b".into();
        assert_eq!(got, want);
    }

    #[test]
    fn optional_fields_default_and_null_is_none() {
        let Scan::Asked(got) =
            scan_output("PG_QUESTION: {\"text\":\"t\",\"default\":null,\"deadline_s\":null}")
        else {
            panic!("expected a question");
        };
        let mut want = q(None, None, &[]);
        want.text = "t".into();
        assert_eq!(got, want);
    }

    #[test]
    fn no_line_is_none_and_bad_lines_are_malformed() {
        assert_eq!(scan_output("hello\nworld"), Scan::None);
        assert_eq!(scan_output(""), Scan::None);
        assert!(matches!(
            scan_output("PG_QUESTION: nope"),
            Scan::Malformed(_)
        ));
        assert!(matches!(scan_output("PG_QUESTION:"), Scan::Malformed(_)));
        assert!(matches!(
            scan_output("PG_QUESTION: {\"text\":\"  \"}"),
            Scan::Malformed(_)
        ));
        // The last line wins even when it is the broken one.
        assert!(matches!(
            scan_output("PG_QUESTION: {\"text\":\"ok\"}\nPG_QUESTION: {"),
            Scan::Malformed(_)
        ));
    }

    #[test]
    fn a_default_needs_a_deadline_and_no_risk_tag() {
        let kept = q(Some("d"), Some(9), &[]).normalized();
        assert_eq!(kept.default.as_deref(), Some("d"));
        assert_eq!(q(Some("d"), None, &[]).normalized().default, None);
        assert_eq!(q(Some("d"), Some(9), &["money"]).normalized().default, None);
    }

    #[test]
    fn wait_plan_uses_default_only_inside_the_cap() {
        let cap = Duration::from_secs(100);
        let p = plan_wait(&q(Some("d"), Some(10), &[]), cap);
        assert_eq!(p.wait, Duration::from_secs(10));
        assert_eq!(p.on_timer, OnTimer::UseDefault("d".into()));
        // Past the cap: park at the cap, the default is never reached.
        let p = plan_wait(&q(Some("d"), Some(101), &[]), cap);
        assert_eq!((p.wait, p.on_timer), (cap, OnTimer::Park));
        // No default, no deadline, or risk-tagged (after normalizing): cap and park.
        for raw in [
            q(None, Some(3), &[]),
            q(Some("d"), None, &[]),
            q(Some("d"), Some(3), &["auth"]),
        ] {
            let p = plan_wait(&raw.normalized(), cap);
            assert_eq!((p.wait, p.on_timer), (cap, OnTimer::Park));
        }
    }

    #[test]
    fn answers_are_appended_in_order() {
        assert_eq!(prompt_with_answers("do it", &[]), "do it");
        let p = prompt_with_answers("do it", &["a".into(), "b".into()]);
        assert_eq!(
            p,
            "do it\n\nAnswer to your question: a\n\nAnswer to your question: b"
        );
    }
}
